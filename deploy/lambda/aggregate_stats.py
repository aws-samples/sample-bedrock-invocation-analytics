# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda Rollup: Summarize HOURLY → DAILY → MONTHLY aggregations.
Triggered by EventBridge schedule.

Watermark catch-up:
  EventBridge is at-least-once and a missed tick (Lambda failure, schedule pause, region
  outage) leaves a permanent gap. Both daily and monthly handlers find the latest existing
  TOTAL record per account and roll forward through every missing period up to "yesterday"
  (daily) or "last completed month" (monthly). Re-running an existing period is safe — the
  aggregate is a covering put-item, idempotent.

Provenance fields on each written record:
  computed_at  : ISO timestamp the rollup ran
  source_count : number of input records aggregated (hour-buckets for DAILY, day-buckets
                 for MONTHLY) — lets a partial/empty rollup be spotted at a glance.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
AGG_TABLE = os.environ["USAGE_STATS_TABLE"]
table = dynamodb.Table(AGG_TABLE)

# How far back to look when seeding the watermark for an account that has no rollup yet
# (e.g. brand-new spoke). Bounds the worst-case backfill on first run.
MAX_BACKFILL_DAYS = 90
MAX_BACKFILL_MONTHS = 12


def handler(event, context):
    rollup_type = event.get("type", "daily")
    now = datetime.now(timezone.utc)

    if rollup_type == "daily":
        # Manual override for backfill: {"type":"daily","date":"2026-03-20"}
        date_str = event.get("date")
        if date_str:
            target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            rollup_daily(target)
        else:
            catch_up_daily(now)
    elif rollup_type == "monthly":
        # Manual override: {"type":"monthly","month":"2026-03"}
        month_str = event.get("month")
        if month_str:
            y, m = month_str.split("-")
            rollup_monthly(int(y), int(m))
        else:
            catch_up_monthly(now)


def catch_up_daily(now):
    """For each account: roll up every missing day from latest DAILY watermark up to yesterday."""
    yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    floor = yesterday - timedelta(days=MAX_BACKFILL_DAYS)
    for pk in get_accounts():
        watermark = _latest_period(pk, "DAILY")  # "YYYY-MM-DD" or None
        if watermark:
            cursor = datetime.strptime(watermark, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        else:
            cursor = floor
        if cursor < floor:
            cursor = floor
        rolled = 0
        while cursor <= yesterday:
            rollup_daily_for_account(pk, cursor)
            cursor += timedelta(days=1)
            rolled += 1
        if rolled > 1:
            print(f"[CATCHUP] daily {pk}: rolled {rolled} days (watermark={watermark})")


def catch_up_monthly(now):
    """For each account: roll up every missing month from latest MONTHLY watermark up to last completed month."""
    # Last completed month = month before "now"
    last_y, last_m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    floor_y, floor_m = _months_ago(last_y, last_m, MAX_BACKFILL_MONTHS)

    for pk in get_accounts():
        watermark = _latest_period(pk, "MONTHLY")  # "YYYY-MM" or None
        if watermark:
            wy, wm = (int(x) for x in watermark.split("-"))
            cy, cm = _next_month(wy, wm)
        else:
            cy, cm = floor_y, floor_m
        if (cy, cm) < (floor_y, floor_m):
            cy, cm = floor_y, floor_m

        rolled = 0
        while (cy, cm) <= (last_y, last_m):
            rollup_monthly(cy, cm)
            cy, cm = _next_month(cy, cm)
            rolled += 1
        if rolled > 1:
            print(f"[CATCHUP] monthly {pk}: rolled {rolled} months (watermark={watermark})")


def rollup_daily(target_date):
    """Aggregate 24 HOURLY records into DAILY for all accounts."""
    for pk in get_accounts():
        rollup_daily_for_account(pk, target_date)


def rollup_daily_for_account(pk, target_date):
    date_str = target_date.strftime("%Y-%m-%d")
    items = _paginated_query(pk, f"HOURLY#{date_str}T00", f"HOURLY#{date_str}T23\xff")
    source_count = len({i["SK"].split("#", 2)[1] for i in items})  # distinct hour buckets
    _aggregate_and_write(items, pk, f"DAILY#{date_str}", ttl_days=365, source_count=source_count)


def rollup_monthly(year, month):
    """Aggregate DAILY records into MONTHLY for all accounts.

    Refuses to roll up the current (in-progress) month or any future month — MONTHLY
    is an archive of *completed* months. The current month's data must be served from
    DAILY at query time. Without this guard, a manual trigger (or schedule misfire)
    would freeze a partial-month snapshot that never updates.
    """
    now = datetime.now(timezone.utc)
    if (year, month) >= (now.year, now.month):
        print(f"[ABORT] rollup_monthly refused: {year:04d}-{month:02d} is current/future "
              f"(now={now.strftime('%Y-%m')}). MONTHLY is for completed months only.")
        return

    month_str = f"{year:04d}-{month:02d}"
    for pk in get_accounts():
        items = _paginated_query(pk, f"DAILY#{month_str}-01", f"DAILY#{month_str}-31\xff")
        source_count = len({i["SK"].split("#", 2)[1] for i in items})  # distinct day buckets
        _aggregate_and_write(items, pk, f"MONTHLY#{month_str}", ttl_days=None, source_count=source_count)


def _latest_period(pk, granularity: str) -> str | None:
    """Return the period segment of the most recent {granularity}#...#TOTAL row, or None.

    SKs sort lexicographically and TOTAL is the last dimension; querying with
    ScanIndexForward=False and Limit=1 grabs the newest record cheaply.
    """
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(f"{granularity}#"),
        ScanIndexForward=False,
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    parts = items[0]["SK"].split("#", 2)
    return parts[1] if len(parts) >= 2 else None


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _months_ago(y: int, m: int, n: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) - n
    return idx // 12, idx % 12 + 1


def _paginated_query(pk, sk_start, sk_end):
    """Query with pagination to handle >1MB results."""
    items = []
    kwargs = {"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").between(sk_start, sk_end)}
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def _aggregate_and_write(items, pk, sk_prefix, ttl_days, source_count):
    """Sum items by dimension, write aggregated records with computed_at + source_count."""
    agg = defaultdict(lambda: defaultdict(int))

    for item in items:
        sk = item["SK"]
        # Extract dimension: everything after the 3rd # (HOURLY#date#DIMENSION or DAILY#date#DIMENSION)
        parts = sk.split("#", 2)
        if len(parts) < 3:
            continue
        dimension = parts[2]  # e.g. MODEL#claude-3-5-haiku, CALLER#wsadmin, TOTAL
        # Normalize ARN-format model IDs
        if dimension.startswith("MODEL#arn:"):
            dimension = "MODEL#" + dimension.rsplit("/", 1)[-1]

        for field in ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                      "cost_micro_usd", "cost_input_micro", "cost_output_micro", "cost_cache_read_micro", "cost_cache_write_micro",
                      "latency_sum_ms", "tpot_sum", "tpot_count"):
            agg[dimension][field] += int(item.get(field, 0))
        # max_latency_ms / tpot_max: take the max
        for f in ("max_latency_ms", "tpot_max"):
            agg[dimension][f] = max(agg[dimension].get(f, 0), int(item.get(f, 0)))
        # min_latency_ms / tpot_min: take the min (ignore 0)
        for f in ("min_latency_ms", "tpot_min"):
            item_val = int(item.get(f, 0))
            if item_val > 0:
                cur = agg[dimension].get(f, 0)
                agg[dimension][f] = item_val if cur == 0 else min(cur, item_val)

    ttl_val = int(time.time()) + ttl_days * 86400 if ttl_days else None
    computed_at = datetime.now(timezone.utc).isoformat()

    with table.batch_writer() as batch:
        for dimension, values in agg.items():
            record = {
                "PK": pk,
                "SK": f"{sk_prefix}#{dimension}",
                "computed_at": computed_at,
                "source_count": source_count,
                **values,
            }
            if ttl_val:
                record["ttl"] = ttl_val
            batch.put_item(Item=record)


def get_accounts():
    """Get all registered account#region PKs."""
    resp = table.query(
        KeyConditionExpression=Key("PK").eq("META") & Key("SK").begins_with("ACCOUNT#"),
    )
    return [item["SK"].replace("ACCOUNT#", "") for item in resp.get("Items", [])]
