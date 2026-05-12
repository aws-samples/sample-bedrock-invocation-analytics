# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda L2: Read usage events from Iceberg, compute cost, aggregate into DDB.

V3 pipeline Stage 2. Triggered every `window_minutes` by EventBridge.

Flow:
  Athena SELECT FROM usage_events WHERE ts in [now-2w, now-w) AND error_code IS NULL
    → per event: lookup pricing → compute cost → TransactWriteItems (dedup + 3-4 aggregate ADDs)
    → write META/L2#latest checkpoint

Idempotency:
  - Firehose upsert (unique_keys=[request_id]) dedupes at the Iceberg layer
  - DDB TransactWriteItems with dedup record (PK=DEDUP, SK=request_id, TTL 24h) prevents
    double-counting across overlapping L2 windows
"""

import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

USAGE_STATS_TABLE = os.environ["USAGE_STATS_TABLE"]
PRICING_TABLE = os.environ["MODEL_PRICING_TABLE"]
ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
ATHENA_OUTPUT_S3 = os.environ["ATHENA_OUTPUT_S3"]
ICEBERG_CATALOG = os.environ["ICEBERG_CATALOG"]  # e.g. s3tablescatalog/bedrock-analytics-xxx
ICEBERG_DATABASE = os.environ.get("ICEBERG_DATABASE", "bedrock_analytics")
ICEBERG_TABLE = os.environ.get("ICEBERG_TABLE", "usage_events")
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "5"))
DEDUP_TTL_HOURS = int(os.environ.get("DEDUP_TTL_HOURS", "24"))

athena = boto3.client("athena")
ddb_resource = boto3.resource("dynamodb")
ddb_client = boto3.client("dynamodb")
usage_table = ddb_resource.Table(USAGE_STATS_TABLE)
pricing_table = ddb_resource.Table(PRICING_TABLE)

# pricing cache keyed by (model_id, day)
_pricing_cache: dict[tuple[str, str], dict] = {}


def handler(event, context):
    """EventBridge scheduled trigger (every WINDOW_MINUTES)."""
    now = datetime.now(timezone.utc)
    # Fixed window: [now - 2w, now - w) — overlap with previous run to catch late arrivals
    window_end = now - timedelta(minutes=WINDOW_MINUTES)
    window_start = window_end - timedelta(minutes=WINDOW_MINUTES * 2)

    # Allow manual override for backfill: {"window_start": "...", "window_end": "..."}
    if event.get("window_start"):
        window_start = datetime.fromisoformat(event["window_start"].replace("Z", "+00:00"))
    if event.get("window_end"):
        window_end = datetime.fromisoformat(event["window_end"].replace("Z", "+00:00"))

    print(f"L2 window: [{window_start.isoformat()}, {window_end.isoformat()})")

    events = query_events(window_start, window_end)
    print(f"Athena returned {len(events)} events")

    processed, skipped_dup, errors = 0, 0, 0
    for ev in events:
        try:
            if aggregate_event(ev):
                processed += 1
            else:
                skipped_dup += 1
        except Exception as e:
            errors += 1
            print(f"Error aggregating event {ev.get('request_id')}: {e}")

    write_checkpoint(window_end, now, processed, skipped_dup, errors)
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "events_read": len(events),
        "processed": processed,
        "skipped_dup": skipped_dup,
        "errors": errors,
    }


def query_events(window_start, window_end):
    """Run Athena SELECT over Iceberg table, return list of dicts.

    Filters:
      - ts in window
      - error_code IS NULL (don't aggregate failed calls)
    """
    sql = f"""
    SELECT account_id, region, request_id, ts, operation, model_id, caller,
           input_tokens, output_tokens, cache_read_tokens,
           cache_write_5m_tokens, cache_write_1h_tokens, cache_write_total_tokens,
           latency_ms
    FROM "{ICEBERG_CATALOG}"."{ICEBERG_DATABASE}"."{ICEBERG_TABLE}"
    WHERE ts >= from_iso8601_timestamp('{window_start.isoformat()}')
      AND ts <  from_iso8601_timestamp('{window_end.isoformat()}')
      AND error_code IS NULL
    """
    qid = athena.start_query_execution(
        QueryString=sql,
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT_S3},
        WorkGroup=ATHENA_WORKGROUP,
    )["QueryExecutionId"]

    # Poll until terminal state (max ~50s)
    deadline = time.time() + 50
    while time.time() < deadline:
        state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {qid} {state}: {reason}")
        time.sleep(0.5)
    else:
        raise TimeoutError(f"Athena query {qid} did not finish in 50s")

    # Paginate results
    events = []
    paginator = athena.get_paginator("get_query_results")
    header = None
    for page in paginator.paginate(QueryExecutionId=qid):
        rows = page["ResultSet"]["Rows"]
        for i, row in enumerate(rows):
            cells = [c.get("VarCharValue") for c in row["Data"]]
            if header is None:
                header = cells
                continue
            events.append(dict(zip(header, cells)))
    return events


def aggregate_event(ev):
    """Process one event: pricing lookup → cost → TransactWriteItems.

    Returns True if aggregated, False if skipped as duplicate.
    """
    account_id = ev.get("account_id") or ""
    region = ev.get("region") or ""
    request_id = ev.get("request_id") or ""
    ts_str = ev.get("ts") or ""
    model_id = ev.get("model_id") or "unknown"
    caller = ev.get("caller") or ""
    operation = ev.get("operation") or ""  # noqa: F841 (reserved for future OPERATION# dim)

    input_tokens = _int(ev.get("input_tokens"))
    output_tokens = _int(ev.get("output_tokens"))
    cache_read_tokens = _int(ev.get("cache_read_tokens"))
    cache_write_total = _int(ev.get("cache_write_total_tokens"))
    cache_write_1h = _int(ev.get("cache_write_1h_tokens"))
    cache_write_5m = _int(ev.get("cache_write_5m_tokens"))
    # If the split fields are missing (Converse API), treat total as 5m
    if ev.get("cache_write_5m_tokens") is None and ev.get("cache_write_1h_tokens") is None:
        cache_write_5m = cache_write_total
        cache_write_1h = 0
    latency_ms = _int(ev.get("latency_ms"))

    # Athena returns TIMESTAMP as "YYYY-MM-DD HH:MM:SS[.ffffff] [TZ]" (space, not T).
    # Normalize to ISO so hour_key and pricing lookup match the rest of the stack.
    iso_ts = _athena_ts_to_iso(ts_str)
    if not iso_ts:
        print(f"Bad ts {ts_str} for {request_id}")
        return False
    hour_key = iso_ts[:13]  # "YYYY-MM-DDTHH"

    # Pricing lookup (expects ISO timestamp so SK range compare works)
    pricing = get_pricing(model_id, iso_ts)

    # Cost in micro-USD (matches V2 semantics — micro-USD per 1k tokens)
    cost_input = input_tokens * pricing["input"] // 1000
    cost_output = output_tokens * pricing["output"] // 1000
    cost_cache_read = cache_read_tokens * pricing["cache_read"] // 1000
    cost_cache_write_5m = cache_write_5m * pricing["cache_write"] // 1000
    cost_cache_write_1h = cache_write_1h * pricing["cache_write_1h"] // 1000
    cost_cache_write = cost_cache_write_5m + cost_cache_write_1h
    cost_total = cost_input + cost_output + cost_cache_read + cost_cache_write

    # TPOT (per output token latency in micro-ms, matches V2)
    has_tpot = output_tokens > 0 and latency_ms > 0
    tpot_micro = round(latency_ms * 1000 / output_tokens) if has_tpot else 0

    pk = f"{account_id}#{region}"
    dedup_ttl = int(time.time()) + DEDUP_TTL_HOURS * 3600
    agg_ttl = int(time.time()) + 90 * 86400

    dims = [f"MODEL#{model_id}", "TOTAL"]
    if caller:
        dims.append(f"CALLER#{caller}")

    # Build TransactWriteItems: [dedup] + 2-3 ADD updates
    transact_items = [{
        "Put": {
            "TableName": USAGE_STATS_TABLE,
            "Item": {
                "PK": {"S": "DEDUP"},
                "SK": {"S": request_id},
                "ttl": {"N": str(dedup_ttl)},
            },
            "ConditionExpression": "attribute_not_exists(SK)",
        }
    }]

    for dim in dims:
        sk = f"HOURLY#{hour_key}#{dim}"
        update_expr_parts = [
            "ADD invocations :one, input_tokens :inp, output_tokens :out, "
            "cache_read_tokens :cr, cache_write_tokens :cw, "
            "cost_micro_usd :cost, cost_input_micro :ci, cost_output_micro :co, "
            "cost_cache_read_micro :ccr, cost_cache_write_micro :ccw, "
            "latency_sum_ms :lat"
        ]
        expr_values = {
            ":one": {"N": "1"},
            ":inp": {"N": str(input_tokens)},
            ":out": {"N": str(output_tokens)},
            ":cr": {"N": str(cache_read_tokens)},
            ":cw": {"N": str(cache_write_total)},
            ":cost": {"N": str(cost_total)},
            ":ci": {"N": str(cost_input)},
            ":co": {"N": str(cost_output)},
            ":ccr": {"N": str(cost_cache_read)},
            ":ccw": {"N": str(cost_cache_write)},
            ":lat": {"N": str(latency_ms)},
            ":ttl": {"N": str(agg_ttl)},
        }
        if has_tpot:
            update_expr_parts[0] += ", tpot_sum :tpot, tpot_count :one"
            expr_values[":tpot"] = {"N": str(tpot_micro)}

        update_expr = "\n".join(update_expr_parts) + " SET #ttl = if_not_exists(#ttl, :ttl)"
        transact_items.append({
            "Update": {
                "TableName": USAGE_STATS_TABLE,
                "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
                "UpdateExpression": update_expr,
                "ExpressionAttributeValues": expr_values,
                "ExpressionAttributeNames": {"#ttl": "ttl"},
            }
        })

    try:
        ddb_client.transact_write_items(TransactItems=transact_items)
    except ddb_client.exceptions.TransactionCanceledException as e:
        # The first TransactItem is the dedup Put with ConditionExpression.
        # If it failed, the event is a duplicate — skip.
        reasons = (getattr(e, "response", {}) or {}).get("CancellationReasons") or []
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            return False
        raise

    # Conditional SET for max/min latency + tpot, one call per dim (non-atomic but idempotent)
    if latency_ms > 0:
        for dim in dims:
            sk = f"HOURLY#{hour_key}#{dim}"
            _conditional_set(pk, sk, "max_latency_ms", latency_ms, op="<")
            _conditional_set(pk, sk, "min_latency_ms", latency_ms, op=">")
            if has_tpot:
                _conditional_set(pk, sk, "tpot_max", tpot_micro, op="<")
                _conditional_set(pk, sk, "tpot_min", tpot_micro, op=">")

    # Auto-register account#region META
    _register_account(pk)
    return True


def _conditional_set(pk, sk, field, val, op):
    """SET field = :val WHERE field is missing OR field {op} :val."""
    try:
        ddb_client.update_item(
            TableName=USAGE_STATS_TABLE,
            Key={"PK": {"S": pk}, "SK": {"S": sk}},
            UpdateExpression=f"SET {field} = :val",
            ConditionExpression=f"attribute_not_exists({field}) OR {field} {op} :val",
            ExpressionAttributeValues={":val": {"N": str(val)}},
        )
    except ddb_client.exceptions.ConditionalCheckFailedException:
        pass


def _register_account(pk):
    now_iso = datetime.now(timezone.utc).isoformat()
    ddb_client.update_item(
        TableName=USAGE_STATS_TABLE,
        Key={"PK": {"S": "META"}, "SK": {"S": f"ACCOUNT#{pk}"}},
        UpdateExpression="SET registered_at = if_not_exists(registered_at, :now), last_seen = :now",
        ExpressionAttributeValues={":now": {"S": now_iso}},
    )


def get_pricing(model_id, ts_str):
    """Look up pricing for model at a given timestamp. Cache per (model, day)."""
    day_key = ts_str[:10]
    cache_key = (model_id, day_key)
    if cache_key in _pricing_cache:
        return _pricing_cache[cache_key]

    result = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0}
    try:
        resp = pricing_table.query(
            KeyConditionExpression=Key("PK").eq(f"MODEL#{model_id}") & Key("SK").lte(ts_str),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            item = items[0]
            result["input"] = int(float(item.get("input_per_1k", 0)) * 1_000_000)
            result["output"] = int(float(item.get("output_per_1k", 0)) * 1_000_000)
            result["cache_read"] = int(float(item.get("cache_read_per_1k", 0)) * 1_000_000)
            result["cache_write"] = int(float(item.get("cache_write_per_1k", 0)) * 1_000_000)
            # Fall back to 5m price if 1h not set
            cw_1h = float(item.get("cache_write_1h_per_1k", 0))
            result["cache_write_1h"] = int(cw_1h * 1_000_000) if cw_1h else result["cache_write"]
    except Exception as e:
        print(f"Pricing lookup failed for {model_id}: {e}")

    _pricing_cache[cache_key] = result
    return result


def write_checkpoint(window_end, processed_at, processed, skipped_dup, errors):
    """Write META/L2#latest checkpoint. Dashboard reads this for 'Data up to' display."""
    usage_table.put_item(Item={
        "PK": "META",
        "SK": "L2#latest",
        "last_window_end": window_end.isoformat(),
        "processed_at": processed_at.isoformat(),
        "events_processed": processed,
        "events_skipped_dup": skipped_dup,
        "events_errors": errors,
    })


def _athena_ts_to_iso(ts_str):
    """Convert Athena TIMESTAMP string 'YYYY-MM-DD HH:MM:SS[.fff] [TZ]' to ISO
    'YYYY-MM-DDTHH:MM:SS' (UTC assumed). Returns None on failure."""
    if not ts_str:
        return None
    try:
        # Drop fractional seconds / timezone suffix, replace space with T
        head = ts_str.split(".", 1)[0].split("+", 1)[0].split(" UTC", 1)[0]
        # Now head is "YYYY-MM-DD HH:MM:SS" (with space)
        return head.replace(" ", "T", 1)
    except Exception:
        return None


def _int(v):
    if v is None or v == "":
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
