# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda Rollup: Summarize HOURLY → DAILY → MONTHLY aggregations.
Triggered by EventBridge schedule.
"""

import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
AGG_TABLE = os.environ["USAGE_STATS_TABLE"]
table = dynamodb.Table(AGG_TABLE)


def handler(event, context):
    rollup_type = event.get("type", "daily")
    now = datetime.now(timezone.utc)

    if rollup_type == "daily":
        # Support custom date via event for backfill: {"type":"daily","date":"2026-03-20"}
        date_str = event.get("date")
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date_str else now - timedelta(days=1)
        rollup_daily(target)
    elif rollup_type == "monthly":
        month_str = event.get("month")  # e.g. "2026-03"
        if month_str:
            y, m = month_str.split("-")
            rollup_monthly(int(y), int(m))
        else:
            first_of_month = now.replace(day=1)
            last_month = first_of_month - timedelta(days=1)
            rollup_monthly(last_month.year, last_month.month)


def rollup_daily(target_date):
    """Aggregate 24 HOURLY records into DAILY for all accounts."""
    date_str = target_date.strftime("%Y-%m-%d")
    accounts = get_accounts()

    for pk in accounts:
        items = _paginated_query(pk, f"HOURLY#{date_str}T00", f"HOURLY#{date_str}T23\xff")
        _aggregate_and_write(items, pk, f"DAILY#{date_str}", ttl_days=365)


def rollup_monthly(year, month):
    """Aggregate DAILY records into MONTHLY for all accounts."""
    month_str = f"{year:04d}-{month:02d}"
    accounts = get_accounts()

    for pk in accounts:
        items = _paginated_query(pk, f"DAILY#{month_str}-01", f"DAILY#{month_str}-31\xff")
        _aggregate_and_write(items, pk, f"MONTHLY#{month_str}", ttl_days=None)


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


def _aggregate_and_write(items, pk, sk_prefix, ttl_days):
    """Sum items by dimension, write aggregated records."""
    import time

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

    with table.batch_writer() as batch:
        for dimension, values in agg.items():
            record = {"PK": pk, "SK": f"{sk_prefix}#{dimension}", **values}
            if ttl_val:
                record["ttl"] = ttl_val
            batch.put_item(Item=record)


def get_accounts():
    """Get all registered account#region PKs."""
    resp = table.query(
        KeyConditionExpression=Key("PK").eq("META") & Key("SK").begins_with("ACCOUNT#"),
    )
    return [item["SK"].replace("ACCOUNT#", "") for item in resp.get("Items", [])]
