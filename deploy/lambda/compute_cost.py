# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda L2: read usage events from Iceberg, price them, aggregate into the DynamoDB serving layer.

Incremental mode (EventBridge schedule):
  Reads events by ingest time (`parsed_at`) from the checkpoint onward, so late or slow-to-deliver
  events are never skipped. Each run re-reads OVERLAP_MINUTES before the checkpoint to cover
  Firehose delivery delay; the DEDUP guard (PK=DEDUP, SK=request_id) keeps re-reads idempotent.
  Any per-event failure fails the run and leaves the checkpoint in place, so the next run retries.

Rebuild mode ({"rebuild": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}):
  Recomputes HOURLY rows for whole hours from Iceberg with current pricing and overwrites them.
  Only hours older than the incremental path's reach are touched, so the two never race.
  Run the daily/monthly rollup afterwards (scripts/rebuild_costs.sh does both).
"""

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

STACK_NAME = os.environ.get("STACK_NAME", "BedrockInvocationAnalytics")
USAGE_STATS_TABLE = os.environ.get("USAGE_STATS_TABLE", "")
PRICING_TABLE = os.environ.get("MODEL_PRICING_TABLE", "")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "")
ICEBERG_CATALOG = os.environ.get("ICEBERG_CATALOG", "")
ICEBERG_DATABASE = os.environ.get("ICEBERG_DATABASE", "bedrock_analytics")
ICEBERG_TABLE = os.environ.get("ICEBERG_TABLE", "usage_events")
DEDUP_TTL_HOURS = int(os.environ.get("DEDUP_TTL_HOURS", "24"))

OVERLAP = timedelta(minutes=15)       # > Firehose buffer (60s) + delivery retries (300s)
SETTLE = timedelta(seconds=60)
MAX_CATCHUP = timedelta(hours=2)      # per run; keeps one run well inside the Lambda timeout
INITIAL_LOOKBACK = timedelta(hours=1)
TS_LOOKBACK = timedelta(hours=24)     # partition-pruning bound on event time for incremental reads
HOURLY_TTL_DAYS = 90

SUM_FIELDS = ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "cost_micro_usd", "cost_input_micro", "cost_output_micro", "cost_cache_read_micro",
              "cost_cache_write_micro", "cost_request_micro", "latency_sum_ms", "tpot_sum", "tpot_count")

_athena = None
_ddb = None
_pricing_cache: dict[tuple[str, str], dict | None] = {}


def athena():
    global _athena
    _athena = _athena or boto3.client("athena")
    return _athena


def ddb():
    global _ddb
    _ddb = _ddb or boto3.client("dynamodb")
    return _ddb


@dataclass
class EventMetrics:
    request_id: str
    pk: str
    hour_key: str
    model_id: str
    dims: list[str]
    sums: dict[str, int]
    latency_ms: int
    tpot_micro: int
    unpriced: bool
    extremes: dict[str, int] = field(default_factory=dict)


def handler(event, context):
    if "rebuild" in event:
        return rebuild(event["rebuild"], datetime.now(timezone.utc))
    return run_incremental(datetime.now(timezone.utc))


# ── Incremental path ──

def plan_window(now: datetime, watermark: datetime | None) -> tuple[datetime, datetime, datetime]:
    """Return (read_start, read_end, new_watermark) on the parsed_at axis."""
    watermark = watermark or (now - INITIAL_LOOKBACK)
    end = min((now - SETTLE).replace(microsecond=0), watermark + MAX_CATCHUP)
    return watermark - OVERLAP, end, max(end, watermark)


def run_incremental(now: datetime):
    checkpoint = read_checkpoint()
    watermark = _parse_iso(checkpoint.get("last_window_end")) if checkpoint else None
    start, end, new_watermark = plan_window(now, watermark)
    print(f"L2 window parsed_at=[{start.isoformat()}, {end.isoformat()})")

    events = query_events(
        f"parsed_at >= {_ts_literal(start)} AND parsed_at < {_ts_literal(end)} "
        f"AND ts >= {_ts_literal(start - TS_LOOKBACK)}"
    )
    fresh = filter_unseen(events)
    stats = {"read": len(events), "processed": 0, "skipped_dup": len(events) - len(fresh),
             "skipped_bad": 0, "errors": 0, "unpriced": 0}
    unpriced_models: set[str] = set()

    for ev in fresh:
        try:
            m = to_metrics(ev, get_pricing)
            if m is None:
                stats["skipped_bad"] += 1
                continue
            if apply_event(m):
                stats["processed"] += 1
                if m.unpriced:
                    stats["unpriced"] += 1
                    unpriced_models.add(m.model_id)
            else:
                stats["skipped_dup"] += 1
        except Exception as e:  # noqa: BLE001 — one bad event must not hide the rest; the run still fails below
            stats["errors"] += 1
            print(f"Error aggregating {ev.get('request_id')}: {e!r}")

    if stats["errors"]:
        emit_metrics(now, watermark or start, stats)
        raise RuntimeError(f"L2 run had {stats['errors']} failed events; checkpoint held at {watermark}")

    advance_checkpoint(new_watermark, now, stats, unpriced_models)
    emit_metrics(now, new_watermark, stats)
    print(json.dumps({"window_end": new_watermark.isoformat(), **stats, "unpriced_models": sorted(unpriced_models)}))
    return {**stats, "window_end": new_watermark.isoformat()}


def filter_unseen(events: list[dict]) -> list[dict]:
    """Drop events that already have a DEDUP marker (cheap BatchGet instead of a cancelled transaction)."""
    by_id = {ev["request_id"]: ev for ev in events if ev.get("request_id")}
    seen: set[str] = set()
    ids = list(by_id)
    for i in range(0, len(ids), 100):
        request = {USAGE_STATS_TABLE: {"Keys": [{"PK": {"S": "DEDUP"}, "SK": {"S": rid}} for rid in ids[i:i + 100]],
                                       "ProjectionExpression": "SK"}}
        while request:
            resp = ddb().batch_get_item(RequestItems=request)
            seen.update(item["SK"]["S"] for item in resp.get("Responses", {}).get(USAGE_STATS_TABLE, []))
            request = resp.get("UnprocessedKeys") or None
            if request:
                time.sleep(0.2)
    return [ev for rid, ev in by_id.items() if rid not in seen]


def apply_event(m: EventMetrics) -> bool:
    """DEDUP put + ADD into every dimension row, atomically. False if the event was already counted."""
    dedup_ttl = int(time.time()) + DEDUP_TTL_HOURS * 3600
    agg_ttl = int(time.time()) + HOURLY_TTL_DAYS * 86400
    items = [{"Put": {
        "TableName": USAGE_STATS_TABLE,
        "Item": {"PK": {"S": "DEDUP"}, "SK": {"S": m.request_id}, "ttl": {"N": str(dedup_ttl)}},
        "ConditionExpression": "attribute_not_exists(SK)",
    }}]
    add_fields = [f for f in SUM_FIELDS if m.sums.get(f)]
    values = {f":{f}": {"N": str(m.sums[f])} for f in add_fields}
    values[":ttl"] = {"N": str(agg_ttl)}
    add_clause = ("ADD " + ", ".join(f"{f} :{f}" for f in add_fields) + " ") if add_fields else ""
    for dim in m.dims:
        items.append({"Update": {
            "TableName": USAGE_STATS_TABLE,
            "Key": {"PK": {"S": m.pk}, "SK": {"S": f"HOURLY#{m.hour_key}#{dim}"}},
            "UpdateExpression": add_clause + "SET #ttl = if_not_exists(#ttl, :ttl)",
            "ExpressionAttributeValues": values,
            "ExpressionAttributeNames": {"#ttl": "ttl"},
        }})
    try:
        ddb().transact_write_items(TransactItems=items)
    except ddb().exceptions.TransactionCanceledException as e:
        reasons = e.response.get("CancellationReasons") or []
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            return False
        raise

    for dim in m.dims:
        sk = f"HOURLY#{m.hour_key}#{dim}"
        for attr, val in m.extremes.items():
            _conditional_set(m.pk, sk, attr, val, "<" if attr in ("max_latency_ms", "tpot_max") else ">")
    _register_account(m.pk)
    return True


def _conditional_set(pk, sk, attr, val, op):
    """SET attr = val when attr is missing or `attr op val` (keeps running max/min)."""
    try:
        ddb().update_item(
            TableName=USAGE_STATS_TABLE,
            Key={"PK": {"S": pk}, "SK": {"S": sk}},
            UpdateExpression=f"SET {attr} = :v",
            ConditionExpression=f"attribute_not_exists({attr}) OR {attr} {op} :v",
            ExpressionAttributeValues={":v": {"N": str(val)}},
        )
    except ddb().exceptions.ConditionalCheckFailedException:
        pass


def _register_account(pk):
    now_iso = datetime.now(timezone.utc).isoformat()
    ddb().update_item(
        TableName=USAGE_STATS_TABLE,
        Key={"PK": {"S": "META"}, "SK": {"S": f"ACCOUNT#{pk}"}},
        UpdateExpression="SET registered_at = if_not_exists(registered_at, :now), last_seen = :now",
        ExpressionAttributeValues={":now": {"S": now_iso}},
    )


def read_checkpoint() -> dict | None:
    item = ddb().get_item(TableName=USAGE_STATS_TABLE, Key={"PK": {"S": "META"}, "SK": {"S": "L2#latest"}}).get("Item")
    return {k: next(iter(v.values())) for k, v in item.items()} if item else None


def advance_checkpoint(window_end, processed_at, stats, unpriced_models):
    """Move the watermark forward only (a slower concurrent run can't drag it back)."""
    try:
        ddb().put_item(
            TableName=USAGE_STATS_TABLE,
            Item={
                "PK": {"S": "META"}, "SK": {"S": "L2#latest"},
                "last_window_end": {"S": window_end.isoformat()},
                "processed_at": {"S": processed_at.isoformat()},
                "events_processed": {"N": str(stats["processed"])},
                "events_skipped_dup": {"N": str(stats["skipped_dup"])},
                "events_unpriced": {"N": str(stats["unpriced"])},
                "unpriced_models": {"S": json.dumps(sorted(unpriced_models)[:20])},
            },
            ConditionExpression="attribute_not_exists(last_window_end) OR last_window_end <= :w",
            ExpressionAttributeValues={":w": {"S": window_end.isoformat()}},
        )
    except ddb().exceptions.ConditionalCheckFailedException:
        print("Checkpoint already ahead; leaving it")


def emit_metrics(now, watermark, stats):
    """CloudWatch Embedded Metric Format; alarms watch CheckpointLagSeconds and UnpricedEvents."""
    print(json.dumps({
        "_aws": {"Timestamp": int(now.timestamp() * 1000), "CloudWatchMetrics": [{
            "Namespace": "BedrockAnalytics", "Dimensions": [["Stack"]],
            "Metrics": [{"Name": "CheckpointLagSeconds", "Unit": "Seconds"},
                        {"Name": "EventsProcessed", "Unit": "Count"},
                        {"Name": "EventErrors", "Unit": "Count"},
                        {"Name": "UnpricedEvents", "Unit": "Count"}],
        }]},
        "Stack": STACK_NAME,
        "CheckpointLagSeconds": int((now - watermark).total_seconds()),
        "EventsProcessed": stats["processed"],
        "EventErrors": stats["errors"],
        "UnpricedEvents": stats["unpriced"],
    }))


# ── Rebuild path ──

def rebuild_bounds(spec: dict, now: datetime, watermark: datetime | None) -> tuple[datetime, datetime]:
    """Whole hours in [start day, end day] that the incremental path can no longer write to."""
    start = datetime.strptime(spec["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(spec.get("end") or spec["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    reach = (watermark or now) - OVERLAP - TS_LOOKBACK - timedelta(hours=1)
    return start, min(end, reach.replace(minute=0, second=0, microsecond=0))


def rebuild(spec: dict, now: datetime):
    checkpoint = read_checkpoint()
    start, end = rebuild_bounds(spec, now, _parse_iso(checkpoint.get("last_window_end")) if checkpoint else None)
    if end <= start:
        return {"rebuilt_rows": 0, "reason": "range is still within the incremental window"}

    events = query_events(f"ts >= {_ts_literal(start)} AND ts < {_ts_literal(end)}", timeout_s=180)
    rows: dict[tuple[str, str], dict[str, int]] = {}
    unpriced: set[str] = set()
    for ev in events:
        m = to_metrics(ev, get_pricing)
        if m is None:
            continue
        if m.unpriced:
            unpriced.add(m.model_id)
        for dim in m.dims:
            row = rows.setdefault((m.pk, f"HOURLY#{m.hour_key}#{dim}"), defaultdict(int))
            merge_into(row, m)

    pks = {pk for pk, _ in rows} | set(list_accounts())
    first_hour, last_hour = start.strftime("%Y-%m-%dT%H"), (end - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
    stale = [(pk, item["SK"]["S"]) for pk in pks
             for item in _query_sk_range(pk, f"HOURLY#{first_hour}", f"HOURLY#{last_hour}\xff")
             if (pk, item["SK"]["S"]) not in rows]

    table = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE)
    expired_cutoff = int(time.time())
    written = 0
    with table.batch_writer() as batch:
        for pk, sk in stale:
            batch.delete_item(Key={"PK": pk, "SK": sk})
        for (pk, sk), values in rows.items():
            hour = datetime.strptime(sk.split("#")[1], "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
            ttl = int(hour.timestamp()) + HOURLY_TTL_DAYS * 86400
            if ttl <= expired_cutoff:
                continue
            batch.put_item(Item={"PK": pk, "SK": sk, "ttl": ttl, "rebuilt_at": now.isoformat(), **values})
            written += 1

    result = {"start": start.isoformat(), "end": end.isoformat(), "events": len(events),
              "rebuilt_rows": written, "deleted_rows": len(stale), "unpriced_models": sorted(unpriced)}
    print(json.dumps(result))
    return result


def merge_into(row: dict, m: EventMetrics):
    for f in SUM_FIELDS:
        row[f] += m.sums.get(f, 0)
    for attr, val in m.extremes.items():
        if attr in ("max_latency_ms", "tpot_max"):
            row[attr] = max(row.get(attr, 0), val)
        else:
            row[attr] = val if not row.get(attr) else min(row[attr], val)


def list_accounts() -> list[str]:
    return [i["SK"]["S"].removeprefix("ACCOUNT#") for i in _query_sk_range("META", "ACCOUNT#", "ACCOUNT#\xff")]


def _query_sk_range(pk, sk_start, sk_end):
    kwargs = {"TableName": USAGE_STATS_TABLE,
              "KeyConditionExpression": "PK = :pk AND SK BETWEEN :a AND :b",
              "ExpressionAttributeValues": {":pk": {"S": pk}, ":a": {"S": sk_start}, ":b": {"S": sk_end}},
              "ProjectionExpression": "SK"}
    while True:
        resp = ddb().query(**kwargs)
        yield from resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ── Shared: event → metrics ──

def to_metrics(ev: dict, pricing_lookup) -> EventMetrics | None:
    """Pure transform of one Iceberg row into additive aggregates. None if the row is unusable."""
    request_id = ev.get("request_id") or ""
    iso_ts = _athena_ts_to_iso(ev.get("ts") or "")
    if not request_id or not iso_ts:
        print(f"Skipping unusable event request_id={request_id!r} ts={ev.get('ts')!r}")
        return None

    model_id = ev.get("model_id") or "unknown"
    caller = ev.get("caller") or ""
    input_tokens = _int(ev.get("input_tokens"))
    output_tokens = _int(ev.get("output_tokens"))
    cache_read = _int(ev.get("cache_read_tokens"))
    cache_write_total = _int(ev.get("cache_write_total_tokens"))
    if ev.get("cache_write_5m_tokens") is None and ev.get("cache_write_1h_tokens") is None:
        cache_write_5m, cache_write_1h = cache_write_total, 0  # Converse API doesn't expose the split
    else:
        cache_write_5m, cache_write_1h = _int(ev.get("cache_write_5m_tokens")), _int(ev.get("cache_write_1h_tokens"))
    latency_ms = _int(ev.get("latency_ms"))

    pricing = pricing_lookup(model_id, iso_ts)
    p = pricing or {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0, "request": 0}
    cost_input = _cost(input_tokens, p["input"])
    cost_output = _cost(output_tokens, p["output"])
    cost_cache_read = _cost(cache_read, p["cache_read"])
    cost_cache_write = _cost(cache_write_5m, p["cache_write"]) + _cost(cache_write_1h, p["cache_write_1h"])
    cost_request = p["request"]

    has_tpot = output_tokens > 0 and latency_ms > 0
    tpot_micro = round(latency_ms * 1000 / output_tokens) if has_tpot else 0
    extremes = {}
    if latency_ms > 0:
        extremes.update(max_latency_ms=latency_ms, min_latency_ms=latency_ms)
    if has_tpot:
        extremes.update(tpot_max=tpot_micro, tpot_min=tpot_micro)

    dims = [f"MODEL#{model_id}", "TOTAL"] + ([f"CALLER#{caller}"] if caller else [])
    return EventMetrics(
        request_id=request_id,
        pk=f"{ev.get('account_id') or ''}#{ev.get('region') or ''}",
        hour_key=iso_ts[:13],
        model_id=model_id,
        dims=dims,
        sums={
            "invocations": 1, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_tokens": cache_read, "cache_write_tokens": cache_write_total,
            "cost_micro_usd": cost_input + cost_output + cost_cache_read + cost_cache_write + cost_request,
            "cost_input_micro": cost_input, "cost_output_micro": cost_output,
            "cost_cache_read_micro": cost_cache_read, "cost_cache_write_micro": cost_cache_write,
            "cost_request_micro": cost_request, "latency_sum_ms": latency_ms,
            "tpot_sum": tpot_micro if has_tpot else 0, "tpot_count": 1 if has_tpot else 0,
        },
        latency_ms=latency_ms,
        tpot_micro=tpot_micro,
        unpriced=pricing is None,
        extremes=extremes,
    )


def get_pricing(model_id: str, iso_ts: str) -> dict | None:
    """Effective price at iso_ts in micro-USD (per 1k tokens, or per request). None if the model has no price."""
    cache_key = (model_id, iso_ts[:10])
    if cache_key in _pricing_cache:
        return _pricing_cache[cache_key]
    items = boto3.resource("dynamodb").Table(PRICING_TABLE).query(
        KeyConditionExpression=Key("PK").eq(f"MODEL#{model_id}") & Key("SK").lte(iso_ts),
        ScanIndexForward=False, Limit=1,
    ).get("Items", [])
    result = None
    if items:
        item = items[0]
        micro = lambda f: int(round(float(item.get(f, 0) or 0) * 1_000_000))  # noqa: E731
        cache_write = micro("cache_write_per_1k")
        result = {
            "input": micro("input_per_1k"), "output": micro("output_per_1k"),
            "cache_read": micro("cache_read_per_1k"), "cache_write": cache_write,
            "cache_write_1h": micro("cache_write_1h_per_1k") or cache_write,
            "request": micro("per_request"),
        }
    _pricing_cache[cache_key] = result
    return result


def query_events(where: str, timeout_s: int = 60) -> list[dict]:
    """Run an Athena SELECT over the Iceberg table (successful calls only) and return rows as dicts."""
    sql = f"""
    SELECT account_id, region, request_id, ts, operation, model_id, caller,
           input_tokens, output_tokens, cache_read_tokens,
           cache_write_5m_tokens, cache_write_1h_tokens, cache_write_total_tokens, latency_ms
    FROM "{ICEBERG_CATALOG}"."{ICEBERG_DATABASE}"."{ICEBERG_TABLE}"
    WHERE {where} AND error_code IS NULL
    """
    qid = athena().start_query_execution(QueryString=sql, WorkGroup=ATHENA_WORKGROUP)["QueryExecutionId"]
    deadline = time.time() + timeout_s
    while True:
        status = athena().get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if status["State"] == "SUCCEEDED":
            break
        if status["State"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Athena query {qid} {status['State']}: {status.get('StateChangeReason', '')}")
        if time.time() > deadline:
            athena().stop_query_execution(QueryExecutionId=qid)
            raise TimeoutError(f"Athena query {qid} exceeded {timeout_s}s")
        time.sleep(0.5)

    rows, header = [], None
    for page in athena().get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        for row in page["ResultSet"]["Rows"]:
            cells = [c.get("VarCharValue") for c in row["Data"]]
            if header is None:
                header = cells
                continue
            rows.append(dict(zip(header, cells)))
    return rows


def _cost(tokens: int, micro_per_1k: int) -> int:
    """Micro-USD, rounded half-up per event (floor division under-counted small calls)."""
    return (tokens * micro_per_1k + 500) // 1000


def _ts_literal(dt: datetime) -> str:
    return f"from_iso8601_timestamp('{dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}')"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _athena_ts_to_iso(ts_str: str) -> str | None:
    """Athena 'YYYY-MM-DD HH:MM:SS[.fff] [UTC]' → 'YYYY-MM-DDTHH:MM:SS' (UTC)."""
    if not ts_str or len(ts_str) < 19:
        return None
    head = ts_str[:19].replace(" ", "T", 1)
    try:
        datetime.strptime(head, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return head


def _int(v) -> int:
    try:
        return int(v) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0
