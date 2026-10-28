# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda Rollup: HOURLY → DAILY → MONTHLY. Triggered by EventBridge.

Each run rolls every account forward from its watermark and also recomputes the most recent
RECOMPUTE_DAYS days (or the last completed month), because L2 can still add late events to
recent hours. Rollups are covering puts, so recomputing is idempotent.

Manual: {"type": "daily", "date": "YYYY-MM-DD"} or {"type": "monthly", "month": "YYYY-MM"}.
"""

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

MAX_BACKFILL_DAYS = 90
MAX_BACKFILL_MONTHS = 12
RECOMPUTE_DAYS = 3

SUM_FIELDS = ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "cost_micro_usd", "cost_input_micro", "cost_output_micro", "cost_cache_read_micro",
              "cost_cache_write_micro", "cost_request_micro", "latency_sum_ms", "tpot_sum", "tpot_count")
MAX_FIELDS = ("max_latency_ms", "tpot_max")
MIN_FIELDS = ("min_latency_ms", "tpot_min")

_table = None


def table():
    global _table
    _table = _table or boto3.resource("dynamodb").Table(os.environ["USAGE_STATS_TABLE"])
    return _table


def handler(event, context):
    now = datetime.now(timezone.utc)
    if event.get("type", "daily") == "daily":
        if event.get("date"):
            day = datetime.strptime(event["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            for pk in get_accounts():
                rollup_daily(pk, day)
        else:
            for pk in get_accounts():
                for day in days_to_roll(now, _latest_period(pk, "DAILY")):
                    rollup_daily(pk, day)
    else:
        if event.get("month"):
            y, m = (int(x) for x in event["month"].split("-"))
            for pk in get_accounts():
                rollup_monthly(pk, y, m, now)
        else:
            for pk in get_accounts():
                for y, m in months_to_roll(now, _latest_period(pk, "MONTHLY")):
                    rollup_monthly(pk, y, m, now)


def days_to_roll(now: datetime, watermark: str | None) -> list[datetime]:
    """Days after the watermark, plus the last RECOMPUTE_DAYS completed days, bounded by MAX_BACKFILL_DAYS."""
    yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    floor = yesterday - timedelta(days=MAX_BACKFILL_DAYS)
    recompute_from = yesterday - timedelta(days=RECOMPUTE_DAYS - 1)
    cursor = recompute_from
    if watermark:
        cursor = min(cursor, datetime.strptime(watermark, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1))
    else:
        cursor = floor
    cursor = max(cursor, floor)
    days = []
    while cursor <= yesterday:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def months_to_roll(now: datetime, watermark: str | None) -> list[tuple[int, int]]:
    """Months after the watermark, always including the last completed month."""
    last = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    floor = _months_ago(*last, MAX_BACKFILL_MONTHS)
    cursor = floor
    if watermark:
        cursor = min(_next_month(*(int(x) for x in watermark.split("-"))), last)
    cursor = max(cursor, floor)
    months = []
    while cursor <= last:
        months.append(cursor)
        cursor = _next_month(*cursor)
    return months


def rollup_daily(pk: str, day: datetime):
    date_str = day.strftime("%Y-%m-%d")
    items = _paginated_query(pk, f"HOURLY#{date_str}T00", f"HOURLY#{date_str}T23\xff")
    source_count = len({i["SK"].split("#", 2)[1] for i in items})
    _aggregate_and_write(items, pk, f"DAILY#{date_str}", ttl_days=365, source_count=source_count)


def rollup_monthly(pk: str, year: int, month: int, now: datetime):
    """MONTHLY holds completed months only; the current month is served from DAILY at query time."""
    if (year, month) >= (now.year, now.month):
        print(f"[SKIP] {year:04d}-{month:02d} is not a completed month")
        return
    month_str = f"{year:04d}-{month:02d}"
    items = _paginated_query(pk, f"DAILY#{month_str}-01", f"DAILY#{month_str}-31\xff")
    source_count = len({i["SK"].split("#", 2)[1] for i in items})
    _aggregate_and_write(items, pk, f"MONTHLY#{month_str}", ttl_days=None, source_count=source_count)


def aggregate(items: list[dict]) -> dict[str, dict[str, int]]:
    """Sum/max/min rows by dimension (the SK part after GRANULARITY#PERIOD#)."""
    agg: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in items:
        parts = item["SK"].split("#", 2)
        if len(parts) < 3:
            continue
        dim = parts[2]
        if dim.startswith("MODEL#arn:"):
            dim = "MODEL#" + dim.rsplit("/", 1)[-1]
        row = agg[dim]
        for f in SUM_FIELDS:
            row[f] += int(item.get(f, 0))
        for f in MAX_FIELDS:
            row[f] = max(row[f], int(item.get(f, 0)))
        for f in MIN_FIELDS:
            v = int(item.get(f, 0))
            if v > 0:
                row[f] = v if row[f] == 0 else min(row[f], v)
    return agg


def _aggregate_and_write(items, pk, sk_prefix, ttl_days, source_count):
    ttl_val = int(time.time()) + ttl_days * 86400 if ttl_days else None
    computed_at = datetime.now(timezone.utc).isoformat()
    with table().batch_writer() as batch:
        for dim, values in aggregate(items).items():
            record = {"PK": pk, "SK": f"{sk_prefix}#{dim}", "computed_at": computed_at,
                      "source_count": source_count, **values}
            if ttl_val:
                record["ttl"] = ttl_val
            batch.put_item(Item=record)


def _latest_period(pk: str, granularity: str) -> str | None:
    """Period of the newest {granularity}# row for pk, or None."""
    items = table().query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(f"{granularity}#"),
        ScanIndexForward=False, Limit=1,
    ).get("Items", [])
    parts = items[0]["SK"].split("#", 2) if items else []
    return parts[1] if len(parts) >= 2 else None


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _months_ago(y: int, m: int, n: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) - n
    return idx // 12, idx % 12 + 1


def _paginated_query(pk, sk_start, sk_end):
    items = []
    kwargs = {"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").between(sk_start, sk_end)}
    while True:
        resp = table().query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def get_accounts() -> list[str]:
    resp = table().query(KeyConditionExpression=Key("PK").eq("META") & Key("SK").begins_with("ACCOUNT#"))
    return [item["SK"].removeprefix("ACCOUNT#") for item in resp.get("Items", [])]
