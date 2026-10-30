# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""DynamoDB / CloudWatch data access for the WebUI API."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone, tzinfo

import boto3
from botocore.config import Config
from boto3.dynamodb.conditions import Key

USAGE_TABLE = os.environ.get("USAGE_STATS_TABLE", "BedrockInvocationAnalytics-usage-stats")
REPRICE_TICK_FUNCTION = os.environ.get("REPRICE_TICK_FUNCTION", "")
REPRICE_KEY = {"PK": "REPRICE", "SK": "latest"}  # the WebUI role may write only this item and only these attributes:
REPRICE_WEBUI_ATTRIBUTES = ("PK", "SK", "job_id", "status", "start", "dry_run", "started_by", "created_at")
PRICING_TABLE = os.environ.get("MODEL_PRICING_TABLE", "BedrockInvocationAnalytics-model-pricing")

_clients: dict = {}


def _usage():
    if "usage" not in _clients:
        _clients["usage"] = boto3.resource("dynamodb").Table(USAGE_TABLE)
    return _clients["usage"]


def _pricing():
    if "pricing" not in _clients:
        _clients["pricing"] = boto3.resource("dynamodb").Table(PRICING_TABLE)
    return _clients["pricing"]


def _lambda():
    if "lambda" not in _clients:
        _clients["lambda"] = boto3.client("lambda", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))
    return _clients["lambda"]


def _cw():
    if "cw" not in _clients:
        _clients["cw"] = boto3.client("cloudwatch")
    return _clients["cw"]


def _plan_segments(start_dt: datetime, end_dt: datetime, daily_end: datetime | None = None) -> list[tuple[str, str, str]]:
    """Plan storage-tier segments covering every UTC hour that overlaps [start_dt, end_dt).

    HOURLY at the partial-day edges, DAILY for whole UTC days before `daily_end` (exclusive; default and cap is
    today's UTC midnight; callers pass the day after the pk's SETTLED watermark), MONTHLY for whole UTC months inside
    the DAILY span. Returns a chronological list of (granularity, sk_start, sk_end), inclusive; empty if invalid.
    """
    s, e = start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc)
    if s >= e:
        return []
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    h0 = s.replace(minute=0, second=0, microsecond=0)
    h1 = e.replace(minute=0, second=0, microsecond=0)
    if h1 < e:
        h1 += timedelta(hours=1)  # exclusive end: the hour containing e is included
    d0 = h0 if h0.hour == 0 else (h0 + timedelta(days=1)).replace(hour=0)
    d1 = min(h1.replace(hour=0), daily_end or today_start, today_start)

    def hourly(a: datetime, b: datetime):
        return [("HOURLY", a.strftime("%Y-%m-%dT%H"), (b - timedelta(hours=1)).strftime("%Y-%m-%dT%H"))] if a < b else []

    def daily(a: datetime, b: datetime):
        return [("DAILY", a.strftime("%Y-%m-%d"), (b - timedelta(days=1)).strftime("%Y-%m-%d"))] if a < b else []

    if d0 >= d1:
        return hourly(h0, h1)
    m0 = d0 if d0.day == 1 else _next_month_start(d0)
    m1 = d1.replace(day=1)
    if m0 < m1:
        middle = daily(d0, m0) + [("MONTHLY", m0.strftime("%Y-%m"), (m1 - timedelta(days=1)).strftime("%Y-%m"))] + daily(m1, d1)
    else:
        middle = daily(d0, d1)
    return hourly(h0, d0) + middle + hourly(d1, h1)


def _next_month_start(dt: datetime) -> datetime:
    """First moment of the next UTC calendar month, after dt's month."""
    base = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if base.month == 12:
        return base.replace(year=base.year + 1, month=1)
    return base.replace(month=base.month + 1)


HOURLY_RETENTION = timedelta(days=89)  # one day inside the 90-day HOURLY TTL


def daily_end_for(account_region: str) -> datetime:
    """Exclusive end of the DAILY tier for a pk: the day after META/SETTLED, or (no watermark) the start of the
    HOURLY retention window so recent days read HOURLY and older days read whatever DAILY exists."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        item = _usage().get_item(Key={"PK": "META", "SK": f"SETTLED#{account_region}"}).get("Item") or {}
        settled = datetime.strptime(item["day"], "%Y-%m-%d").replace(tzinfo=timezone.utc) if item.get("day") else None
    except Exception as e:  # noqa: BLE001 — fall back to the conservative tiering rather than failing the request
        print(f"[WARN] Failed to read SETTLED for {account_region}: {e}")
        settled = None
    if settled:
        return min(settled + timedelta(days=1), today)
    return today - HOURLY_RETENTION


def get_l2_checkpoint() -> dict | None:
    """Read META/L2#latest. Returns None if L2 hasn't run yet (e.g. fresh deploy)."""
    try:
        resp = _usage().get_item(Key={"PK": "META", "SK": "L2#latest"})
    except Exception as e:
        print(f"[WARN] Failed to read L2 checkpoint: {e}")
        return None
    return resp.get("Item")


def unpriced_models() -> list[str]:
    """Models with events counted at $0 (META/UNPRICED#<model>; cleared once a recompute covers them priced)."""
    try:
        items = _usage().query(KeyConditionExpression=Key("PK").eq("META") & Key("SK").begins_with("UNPRICED#"))["Items"]
    except Exception as e:  # noqa: BLE001 — a warning banner must not break the dashboard
        print(f"[WARN] Failed to read unpriced markers: {e}")
        return []
    return sorted(i["SK"].removeprefix("UNPRICED#") for i in items)


def _account_names() -> dict[str, str]:
    """account_id → friendly name, from ACCOUNT_NAMES (JSON object set by the stack from config.yaml)."""
    try:
        return json.loads(os.environ.get("ACCOUNT_NAMES") or "{}")
    except ValueError:
        return {}


def get_accounts() -> list[dict]:
    """Registered account#region list, annotated with friendly names."""
    resp = _usage().query(KeyConditionExpression=Key("PK").eq("META") & Key("SK").begins_with("ACCOUNT#"))
    names = _account_names()
    results = []
    for item in resp.get("Items", []):
        acct_region = item["SK"].replace("ACCOUNT#", "")
        parts = acct_region.split("#", 1)
        acct_id = parts[0]
        results.append({
            "account_id": acct_id,
            "region": parts[1] if len(parts) > 1 else "",
            "key": acct_region,
            "name": names.get(acct_id, ""),
        })
    return results


def query_usage(account_region: str, segments: list[tuple[str, str, str]], dimension_prefix: str = "",
                exact: bool = False) -> list[dict]:
    """Query usage stats across one or more storage-tier segments and concatenate results.

    Args:
        account_region: e.g. "123456789012#us-west-2"
        segments: list of (granularity, sk_start, sk_end) — see _plan_segments()
        dimension_prefix: filter SK by dimension, e.g. "MODEL#", "CALLER#", "TOTAL"
    """
    out = []
    for granularity, start, end in segments:
        sk_start = f"{granularity}#{start}"
        sk_end = f"{granularity}#{end}\xff"

        items: list[dict] = []
        kwargs = {"KeyConditionExpression": Key("PK").eq(account_region) & Key("SK").between(sk_start, sk_end)}
        while True:
            resp = _usage().query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        if dimension_prefix:
            items = [i for i in items if (_extract_dimension(i["SK"]) == dimension_prefix if exact
                                          else _extract_dimension(i["SK"]).startswith(dimension_prefix))]

        out.extend(_format_item(i) for i in items)
    return out


COST_FIELDS = ("cost_usd", "cost_input", "cost_output", "cost_cache_read", "cost_cache_write", "cost_request")
TOKEN_FIELDS = ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def get_dashboard_bundle(account_region: str, start_dt: datetime, end_dt: datetime, tz: tzinfo | None = None) -> dict:
    """Summary, by-model, by-caller and TOTAL trend from a single pass over the storage segments."""
    items = query_usage(account_region, _plan_segments(start_dt, end_dt, daily_end_for(account_region)))
    totals = [i for i in items if i["dimension"] == "TOTAL"]
    return {
        "summary": summarize(totals),
        "models": group_by_model([i for i in items if i["dimension"].startswith("MODEL#")]),
        "callers": group_by_caller([i for i in items if i["dimension"].startswith("CALLER#")]),
        "trend_total": _rebucket(totals, _pick_display_granularity(start_dt, end_dt), tz or timezone.utc),
    }


def get_summary(account_region: str, start_dt: datetime, end_dt: datetime) -> dict:
    return summarize(query_usage(account_region, _plan_segments(start_dt, end_dt, daily_end_for(account_region)), "TOTAL"))


def get_by_model(account_region: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    return group_by_model(query_usage(account_region, _plan_segments(start_dt, end_dt, daily_end_for(account_region)), "MODEL#"))


def get_by_caller(account_region: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    return group_by_caller(query_usage(account_region, _plan_segments(start_dt, end_dt, daily_end_for(account_region)), "CALLER#"))


def summarize(items: list[dict]) -> dict:
    """Totals for the summary cards from TOTAL rows."""
    total = {k: 0 for k in (*TOKEN_FIELDS, "latency_sum_ms", "tpot_sum", "tpot_count")}
    total["cost_usd"] = 0.0
    for item in items:
        for k in total:
            total[k] += item.get(k, 0)
    total["avg_latency_ms"] = round(total["latency_sum_ms"] / total["invocations"]) if total["invocations"] else 0
    total["avg_tpot"] = round(total["tpot_sum"] / total["tpot_count"] / 1000, 2) if total["tpot_count"] else 0
    return total


def group_by_model(items: list[dict]) -> list[dict]:
    models: dict[str, dict] = {}
    for item in items:
        model = item["dimension"].removeprefix("MODEL#")
        m = models.setdefault(model, {"model": model, **{k: 0 for k in TOKEN_FIELDS}, **{k: 0.0 for k in COST_FIELDS},
                                      "latency_sum_ms": 0, "max_latency_ms": 0, "min_latency_ms": 0,
                                      "tpot_max": 0, "tpot_min": 0, "tpot_sum": 0, "tpot_count": 0})
        for k in (*TOKEN_FIELDS, *COST_FIELDS, "latency_sum_ms", "tpot_sum", "tpot_count"):
            m[k] += item.get(k, 0)
        m["max_latency_ms"] = max(m["max_latency_ms"], item.get("max_latency_ms", 0))
        m["tpot_max"] = max(m["tpot_max"], item.get("tpot_max", 0))
        for f in ("min_latency_ms", "tpot_min"):
            v = item.get(f, 0)
            if v > 0:
                m[f] = v if m[f] == 0 else min(m[f], v)
    for m in models.values():
        m["avg_latency_ms"] = round(m["latency_sum_ms"] / m["invocations"]) if m["invocations"] else 0
        m["tpot_avg"] = round(m["tpot_sum"] / m["tpot_count"] / 1000, 2) if m["tpot_count"] else 0
    return sorted(models.values(), key=lambda x: x["cost_usd"], reverse=True)


def group_by_caller(items: list[dict]) -> list[dict]:
    callers: dict[str, dict] = {}
    for item in items:
        caller = item["dimension"].removeprefix("CALLER#")
        c = callers.setdefault(caller, {"caller": caller, **{k: 0 for k in TOKEN_FIELDS}, **{k: 0.0 for k in COST_FIELDS}})
        for k in (*TOKEN_FIELDS, *COST_FIELDS):
            c[k] += item.get(k, 0)
    return sorted(callers.values(), key=lambda x: x["cost_usd"], reverse=True)


def get_trend(account_region: str, start_dt: datetime, end_dt: datetime,
              dimension: str = "TOTAL", tz: tzinfo | None = None) -> list[dict]:
    """Get a contiguous time-series in a single display granularity.

    Internally this queries the underlying mixed-granularity buckets via _plan_segments
    and rolls them up to one target granularity (auto-picked from span). This means a
    "This Year" view always returns equally-spaced monthly buckets — including a synthesised
    bucket for the current month built from its DAILY+HOURLY rows. Storage stays sparse;
    nothing here is persisted.

    Bucketing is done in `tz` (defaulting to UTC) so the user-facing month/day boundaries
    match what the user selected. DAILY/MONTHLY raw rows have no sub-day precision, so they
    are attributed to the tz-day/tz-month containing their UTC start instant — at most an
    8-hour edge offset for tz like Asia/Singapore, which is the standard tradeoff for
    UTC-bucketed storage.
    """
    target = _pick_display_granularity(start_dt, end_dt)
    items = query_usage(account_region, _plan_segments(start_dt, end_dt, daily_end_for(account_region)), dimension, exact=True)
    return _rebucket(items, target, tz or timezone.utc)


def _pick_display_granularity(start_dt: datetime, end_dt: datetime) -> str:
    span_days = (end_dt - start_dt).total_seconds() / 86400
    if span_days <= 2:
        return "HOURLY"
    if span_days <= 90:
        return "DAILY"
    return "MONTHLY"


def _bucket_key(period: str, src_granularity: str, target: str, tz: tzinfo) -> str:
    """Map a raw row's period to its target-granularity key in `tz`.

    Raw periods are UTC strings: 'YYYY-MM-DDTHH', 'YYYY-MM-DD', 'YYYY-MM'.
    We parse to a UTC instant (start-of-bucket), shift to tz, then truncate.
    """
    if src_granularity == "HOURLY":
        dt = datetime.strptime(period, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    elif src_granularity == "DAILY":
        dt = datetime.strptime(period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif src_granularity == "MONTHLY":
        dt = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
    else:
        return period

    local = dt.astimezone(tz)
    if target == "HOURLY":
        return local.strftime("%Y-%m-%dT%H")
    if target == "DAILY":
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m")


def _rebucket(rows: list[dict], target: str, tz: tzinfo) -> list[dict]:
    """Aggregate mixed-granularity rows into a single-granularity series, grouped by tz.

    Same per-field rules as the rollup lambda (sum / max / min, recompute averages).
    Output rows preserve the same keys as raw rows so chart code doesn't need to special-case.
    """
    if not rows:
        return []

    SUMMABLE = ("invocations", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "cost_micro_usd", "cost_input", "cost_output",
                "cost_cache_read", "cost_cache_write", "cost_request", "latency_sum_ms",
                "tpot_sum", "tpot_count", "ttft_avg", "ttft_p99")

    groups: dict[str, dict] = {}
    for row in rows:
        key = _bucket_key(row.get("period", ""), row.get("granularity", ""), target, tz)
        if not key:
            continue
        agg = groups.setdefault(key, {
            "period": key,
            "dimension": row.get("dimension", ""),
            "max_latency_ms": 0, "min_latency_ms": 0,
            "tpot_max": 0, "tpot_min": 0,
            **{k: 0 for k in SUMMABLE},
        })
        for k in SUMMABLE:
            agg[k] += row.get(k, 0)
        agg["max_latency_ms"] = max(agg["max_latency_ms"], row.get("max_latency_ms", 0))
        agg["tpot_max"] = max(agg["tpot_max"], row.get("tpot_max", 0))
        for f in ("min_latency_ms", "tpot_min"):
            v = row.get(f, 0)
            if v > 0:
                cur = agg[f]
                agg[f] = v if cur == 0 else min(cur, v)

    # Recompute derived averages and surface cost_usd from cost_micro_usd
    out = []
    for key in sorted(groups):
        a = groups[key]
        a["cost_usd"] = a["cost_micro_usd"] / 1_000_000
        a["avg_latency_ms"] = round(a["latency_sum_ms"] / a["invocations"]) if a["invocations"] else 0
        a["tpot_avg"] = round(a["tpot_sum"] / a["tpot_count"] / 1000, 2) if a["tpot_count"] else 0
        out.append(a)
    return out


def _extract_dimension(sk: str) -> str:
    """Extract dimension from SK like HOURLY#2026-03-23T05#MODEL#xxx → MODEL#xxx"""
    parts = sk.split("#", 2)
    return parts[2] if len(parts) >= 3 else ""


PRICE_FIELDS = ("input_per_1k", "output_per_1k", "cache_read_per_1k", "cache_write_per_1k",
                "cache_write_1h_per_1k", "per_request")


def _pricing_record(model_id: str, item: dict) -> dict:
    return {"model_id": model_id, "effective_date": item["SK"], "source": item.get("source", ""),
            "updated_by": item.get("updated_by", ""), **{f: float(item.get(f, 0) or 0) for f in PRICE_FIELDS}}


def get_all_pricing() -> list[dict]:
    """Current effective price per model (latest SK). Full scan; the table holds ~1K items."""
    resp = _pricing().scan()
    items = resp.get("Items", [])
    while resp.get("LastEvaluatedKey"):
        resp = _pricing().scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    latest: dict[str, dict] = {}
    for item in items:
        pk = item["PK"]
        if pk.startswith("MODEL#") and (pk not in latest or item["SK"] > latest[pk]["SK"]):
            latest[pk] = item
    return [_pricing_record(pk.removeprefix("MODEL#"), item) for pk, item in sorted(latest.items())]


def get_pricing_sync_info() -> dict | None:
    """Get last pricing sync metadata."""
    try:
        resp = _usage().get_item(Key={"PK": "META", "SK": "PRICING_SYNC#latest"})
        return resp.get("Item")
    except Exception:
        return None


def get_ttft_trend(model_id: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Get TimeToFirstToken trend from CloudWatch for a model."""
    span_days = (end_dt - start_dt).total_seconds() / 86400
    period = 3600 if span_days <= 7 else 86400
    try:
        resp = _cw().get_metric_data(
            MetricDataQueries=[
                {"Id": "avg", "MetricStat": {"Metric": {"Namespace": "AWS/Bedrock", "MetricName": "TimeToFirstToken", "Dimensions": [{"Name": "ModelId", "Value": model_id}]}, "Period": period, "Stat": "Average"}},
                {"Id": "p99", "MetricStat": {"Metric": {"Namespace": "AWS/Bedrock", "MetricName": "TimeToFirstToken", "Dimensions": [{"Name": "ModelId", "Value": model_id}]}, "Period": period, "Stat": "p99"}},
            ],
            StartTime=start_dt.astimezone(timezone.utc),
            EndTime=end_dt.astimezone(timezone.utc),
        )
    except Exception as e:
        print(f"[WARN] CloudWatch TTFT query failed: {e}")
        return []

    results = {r["Id"]: dict(zip(r["Timestamps"], r["Values"])) for r in resp.get("MetricDataResults", [])}
    timestamps = sorted(set(results.get("avg", {}).keys()) | set(results.get("p99", {}).keys()))
    return [{"timestamp": t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
             "ttft_avg": round(results.get("avg", {}).get(t, 0)),
             "ttft_p99": round(results.get("p99", {}).get(t, 0))} for t in timestamps]


def save_pricing(model_id: str, prices: dict[str, float], effective_date: str, updated_by: str):
    """Save a manual pricing record with every price field, so cache and per-request prices aren't dropped."""
    _pricing().put_item(Item={
        "PK": f"MODEL#{model_id}",
        "SK": effective_date,
        **{f: format(round(float(prices.get(f) or 0), 9), "f").rstrip("0").rstrip(".") or "0" for f in PRICE_FIELDS},
        "source": "manual",
        "updated_by": updated_by,
    })


def get_pricing_history(model_id: str) -> list[dict]:
    """All pricing records for a model, newest first."""
    resp = _pricing().query(KeyConditionExpression=Key("PK").eq(f"MODEL#{model_id}"), ScanIndexForward=False)
    return [_pricing_record(model_id, item) for item in resp.get("Items", [])]


def delete_pricing(model_id: str, effective_date: str):
    """Delete a pricing record."""
    _pricing().delete_item(Key={"PK": f"MODEL#{model_id}", "SK": effective_date})


def _format_item(item: dict) -> dict:
    """Convert DynamoDB item to clean dict.

    `granularity` is extracted from the SK and exposed so callers (e.g. _rebucket)
    know whether `period` is "YYYY-MM-DDTHH" (HOURLY), "YYYY-MM-DD" (DAILY), or
    "YYYY-MM" (MONTHLY) without re-parsing.
    """
    sk = item["SK"]
    parts = sk.split("#", 2)
    granularity = parts[0] if parts else ""
    period = parts[1] if len(parts) >= 2 else ""
    dimension = parts[2] if len(parts) >= 3 else ""

    cost_micro = int(item.get("cost_micro_usd", 0))
    invocations = int(item.get("invocations", 0))
    latency_sum = int(item.get("latency_sum_ms", 0))

    tpot_count = int(item.get("tpot_count", 0))
    tpot_sum = int(item.get("tpot_sum", 0))

    return {
        "granularity": granularity,
        "period": period,
        "dimension": dimension,
        "invocations": invocations,
        "input_tokens": int(item.get("input_tokens", 0)),
        "output_tokens": int(item.get("output_tokens", 0)),
        "cache_read_tokens": int(item.get("cache_read_tokens", 0)),
        "cache_write_tokens": int(item.get("cache_write_tokens", 0)),
        "cost_usd": cost_micro / 1_000_000,
        "cost_micro_usd": cost_micro,
        "cost_input": int(item.get("cost_input_micro", 0)) / 1_000_000,
        "cost_output": int(item.get("cost_output_micro", 0)) / 1_000_000,
        "cost_cache_read": int(item.get("cost_cache_read_micro", 0)) / 1_000_000,
        "cost_cache_write": int(item.get("cost_cache_write_micro", 0)) / 1_000_000,
        "cost_request": int(item.get("cost_request_micro", 0)) / 1_000_000,
        "latency_sum_ms": latency_sum,
        "avg_latency_ms": round(latency_sum / invocations) if invocations else 0,
        "max_latency_ms": int(item.get("max_latency_ms", 0)),
        "min_latency_ms": int(item.get("min_latency_ms", 0)),
        "tpot_sum": tpot_sum,
        "tpot_count": tpot_count,
        "tpot_avg": round(tpot_sum / tpot_count / 1000, 2) if tpot_count else 0,
        "tpot_min": round(int(item.get("tpot_min", 0)) / 1000, 2),
        "tpot_max": round(int(item.get("tpot_max", 0)) / 1000, 2),
    }


# ── Reprice jobs (worker: compute_cost / reprice.py) ──

def get_reprice_job() -> dict | None:
    return _usage().get_item(Key=REPRICE_KEY, ConsistentRead=True).get("Item")


def create_reprice_job(start: str, dry_run: bool, started_by: str) -> tuple[dict | None, bool]:
    """Queue a job unless one is active. Returns (job, created); on conflict the active job."""
    job = {**REPRICE_KEY, "job_id": str(uuid.uuid4()), "status": "queued", "start": start, "dry_run": dry_run,
           "started_by": started_by, "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        _usage().put_item(Item=job, ConditionExpression="attribute_not_exists(PK) OR #s IN (:d, :f, :c)",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":d": "done", ":f": "failed", ":c": "cancelled"})
    except _usage().meta.client.exceptions.ConditionalCheckFailedException:
        return get_reprice_job(), False
    return job, True


def cancel_reprice_job(job_id: str) -> dict | None:
    """queued → cancelled, running → cancelling (the worker finishes the month in progress). None if no such job."""
    job = get_reprice_job()
    if not job or job.get("job_id") != job_id:
        return None
    target = {"queued": "cancelled", "running": "cancelling"}.get(job.get("status"))
    if target:
        try:
            _usage().update_item(Key=REPRICE_KEY, UpdateExpression="SET #s = :t",
                                 ConditionExpression="job_id = :j AND #s = :seen", ExpressionAttributeNames={"#s": "status"},
                                 ExpressionAttributeValues={":t": target, ":j": job_id, ":seen": job["status"]})
        except _usage().meta.client.exceptions.ConditionalCheckFailedException:
            moved = get_reprice_job() or {}
            if job["status"] == "queued" and moved.get("job_id") == job_id and moved.get("status") == "running":
                return cancel_reprice_job(job_id)  # the worker picked it up in between
    return get_reprice_job()


def kick_reprice():
    """Start the worker now instead of at the next schedule; failure only delays the job."""
    if not REPRICE_TICK_FUNCTION:
        return
    try:
        _lambda().invoke(FunctionName=REPRICE_TICK_FUNCTION, InvocationType="Event", Payload=b"{}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Failed to invoke {REPRICE_TICK_FUNCTION}: {e}")
