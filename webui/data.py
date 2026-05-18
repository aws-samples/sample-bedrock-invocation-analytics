# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""DynamoDB data access for WebUI."""

import os
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import boto3
import yaml
from boto3.dynamodb.conditions import Key

USAGE_TABLE = os.environ.get("USAGE_STATS_TABLE", "BedrockInvocationAnalytics-usage-stats")
PRICING_TABLE = os.environ.get("MODEL_PRICING_TABLE", "BedrockInvocationAnalytics-model-pricing")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")

# V3 data layer (Iceberg via Athena federated catalog)
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "")
ICEBERG_CATALOG = os.environ.get("ICEBERG_CATALOG", "")
ICEBERG_DATABASE = os.environ.get("ICEBERG_DATABASE", "bedrock_analytics")
ICEBERG_TABLE = os.environ.get("ICEBERG_TABLE", "usage_events")

_ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
_usage = _ddb.Table(USAGE_TABLE)
_pricing = _ddb.Table(PRICING_TABLE)


def _plan_segments(start_dt: datetime, end_dt: datetime) -> list[tuple[str, str, str]]:
    """Plan storage-tier segments for [start_dt, end_dt) — any input tz, output SKs are UTC.

    The window is shaved progressively from both ends:
      1. HOURLY head  — partial day at the start, until the next UTC midnight
      2. DAILY head   — full UTC days, until the next UTC month boundary
      3. MONTHLY      — full UTC months that are entirely within the window AND completed
      4. DAILY tail   — full UTC days after the last completed month, until today (UTC) starts
      5. HOURLY tail  — today's UTC hours up to e_utc

    Each tier covers only the range it's allowed to (per the rollup contract: MONTHLY is
    completed months only, DAILY is completed days only, today/in-progress hour is HOURLY).
    Returns a chronological list of (granularity, sk_start, sk_end) — empty if invalid.
    """
    s = start_dt.astimezone(timezone.utc)
    e = end_dt.astimezone(timezone.utc)
    if s >= e:
        return []

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cur_month_start = today_start.replace(day=1)

    segments: list[tuple[str, str, str]] = []
    cursor = s  # advances toward e

    # ── 1. HOURLY head: from cursor up to the next UTC midnight (exclusive) ──
    next_midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) \
        if (cursor.hour, cursor.minute, cursor.second, cursor.microsecond) != (0, 0, 0, 0) else cursor
    if cursor < next_midnight:
        head_end = min(next_midnight, e, today_start + timedelta(hours=1))  # don't cross today/end
        segments.append(("HOURLY", cursor.strftime("%Y-%m-%dT%H"),
                         (head_end - timedelta(hours=1)).strftime("%Y-%m-%dT%H")))
        cursor = next_midnight
        if cursor >= e:
            return segments

    # ── 2. DAILY head: full UTC days until the next month boundary (exclusive) or today ──
    # Only run if cursor isn't already month-aligned — otherwise let MONTHLY take over.
    if cursor.day != 1:
        next_month = _next_month_start(cursor)
        daily_head_end = min(next_month, today_start, e)
        if cursor < daily_head_end:
            segments.append(("DAILY", cursor.strftime("%Y-%m-%d"),
                             (daily_head_end - timedelta(days=1)).strftime("%Y-%m-%d")))
            cursor = daily_head_end
            if cursor >= e:
                return segments

    # ── 3. MONTHLY: only complete months that ended before the current UTC month ──
    monthly_upper = min(cur_month_start, e)  # exclusive
    if cursor.day == 1 and cursor < monthly_upper:
        # cursor is at a UTC month start; monthly_upper is also a month start (or e)
        # Last fully-included month is monthly_upper - 1 day's month
        last_full_month_start = (monthly_upper - timedelta(days=1)).replace(day=1)
        if cursor <= last_full_month_start:
            segments.append(("MONTHLY", cursor.strftime("%Y-%m"),
                             last_full_month_start.strftime("%Y-%m")))
            cursor = _next_month_start(last_full_month_start)
            if cursor >= e:
                return segments

    # ── 4. DAILY tail: full UTC days from cursor up to today_start (exclusive) ──
    daily_tail_end = min(today_start, e)
    if cursor < daily_tail_end:
        segments.append(("DAILY", cursor.strftime("%Y-%m-%d"),
                         (daily_tail_end - timedelta(days=1)).strftime("%Y-%m-%d")))
        cursor = daily_tail_end
        if cursor >= e:
            return segments

    # ── 5. HOURLY tail: today's hours up to e ──
    if cursor < e:
        # e_hour_inclusive: include the hour containing e (so an end_dt like 12:50 hits SK ...T12)
        last_hour_inclusive = e if (e.minute, e.second, e.microsecond) == (0, 0, 0) else e + timedelta(hours=1)
        segments.append(("HOURLY", cursor.strftime("%Y-%m-%dT%H"),
                         (last_hour_inclusive - timedelta(hours=1)).strftime("%Y-%m-%dT%H")))

    return segments


def _next_month_start(dt: datetime) -> datetime:
    """First moment of the next UTC calendar month, after dt's month."""
    base = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if base.month == 12:
        return base.replace(year=base.year + 1, month=1)
    return base.replace(month=base.month + 1)


def get_l2_checkpoint() -> dict | None:
    """Read META/L2#latest. Returns None if L2 hasn't run yet (e.g. fresh deploy)."""
    try:
        resp = _usage.get_item(Key={"PK": "META", "SK": "L2#latest"})
    except Exception as e:
        print(f"[WARN] Failed to read L2 checkpoint: {e}")
        return None
    return resp.get("Item")


def _load_config_names() -> dict[str, str]:
    """Map profile → friendly name from config.yaml. Best-effort; returns {} on any failure.
    Also keyed by account_id once resolved (we can't resolve profile → account_id here, so
    dashboard falls back to profile name if account_id lookup fails)."""
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[WARN] Failed to read config.yaml: {e}")
        return {}
    # Map profile → name. account_id isn't in config.yaml, so dashboard will match
    # by calling aws sts get-caller-identity at startup (cheap), but for simplicity
    # we just key by profile and let the caller resolve.
    return {a["profile"]: a.get("name") or a["profile"]
            for a in (cfg.get("accounts") or []) if a.get("profile")}


def _account_id_to_name() -> dict[str, str]:
    """Map account_id → friendly name by resolving each config profile via STS.
    Silent on failures (returns empty map for unresolvable profiles)."""
    profile_names = _load_config_names()
    result = {}
    for profile, name in profile_names.items():
        try:
            sess = boto3.Session(profile_name=profile)
            acct = sess.client("sts").get_caller_identity()["Account"]
            result[acct] = name
        except Exception as e:
            print(f"[WARN] Can't resolve account_id for profile={profile}: {e}")
    return result


_ACCOUNT_NAMES = _account_id_to_name()


def get_accounts() -> list[dict]:
    """Get registered account#region list, annotated with config.yaml friendly name."""
    try:
        resp = _usage.query(
            KeyConditionExpression=Key("PK").eq("META") & Key("SK").begins_with("ACCOUNT#"),
        )
    except Exception as e:
        print(f"[ERROR] Failed to query DynamoDB table '{USAGE_TABLE}' in {AWS_REGION}: {e}")
        return []
    results = []
    for item in resp.get("Items", []):
        acct_region = item["SK"].replace("ACCOUNT#", "")
        parts = acct_region.split("#", 1)
        acct_id = parts[0]
        results.append({
            "account_id": acct_id,
            "region": parts[1] if len(parts) > 1 else "",
            "key": acct_region,
            "name": _ACCOUNT_NAMES.get(acct_id, ""),
        })
    return results


def query_usage(account_region: str, segments: list[tuple[str, str, str]], dimension_prefix: str = "") -> list[dict]:
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
            resp = _usage.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        if dimension_prefix:
            items = [i for i in items if _extract_dimension(i["SK"]).startswith(dimension_prefix)]

        out.extend(_format_item(i) for i in items)
    return out


def get_summary(account_region: str, start_dt: datetime, end_dt: datetime) -> dict:
    """Get summary stats for dashboard cards."""
    items = query_usage(account_region, _plan_segments(start_dt, end_dt), "TOTAL")

    total = {"invocations": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0, "latency_sum_ms": 0, "tpot_sum": 0, "tpot_count": 0}
    for item in items:
        for k in ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "tpot_sum", "tpot_count"):
            total[k] += item.get(k, 0)
        total["cost_usd"] += item.get("cost_usd", 0.0)
        total["latency_sum_ms"] += item.get("latency_sum_ms", 0)

    total["avg_latency_ms"] = round(total["latency_sum_ms"] / total["invocations"]) if total["invocations"] else 0
    total["avg_tpot"] = round(total["tpot_sum"] / total["tpot_count"] / 1000, 2) if total["tpot_count"] else 0
    return total


def get_by_model(account_region: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Get usage grouped by model."""
    items = query_usage(account_region, _plan_segments(start_dt, end_dt), "MODEL#")

    # Aggregate across time periods by model
    models = {}
    for item in items:
        model = item["dimension"].replace("MODEL#", "")
        if model not in models:
            models[model] = {"model": model, "invocations": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0, "cost_input": 0.0, "cost_output": 0.0, "cost_cache_read": 0.0, "cost_cache_write": 0.0, "latency_sum_ms": 0, "max_latency_ms": 0, "min_latency_ms": 0, "tpot_max": 0, "tpot_min": 0, "tpot_sum": 0, "tpot_count": 0}
        for k in ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "latency_sum_ms", "tpot_sum", "tpot_count"):
            models[model][k] += item.get(k, 0)
        for k in ("cost_usd", "cost_input", "cost_output", "cost_cache_read", "cost_cache_write"):
            models[model][k] += item.get(k, 0.0)
        models[model]["max_latency_ms"] = max(models[model]["max_latency_ms"], item.get("max_latency_ms", 0))
        models[model]["tpot_max"] = max(models[model]["tpot_max"], item.get("tpot_max", 0))
        for f in ("min_latency_ms", "tpot_min"):
            v = item.get(f, 0)
            if v > 0:
                cur = models[model][f]
                models[model][f] = v if cur == 0 else min(cur, v)

    for m in models.values():
        m["avg_latency_ms"] = round(m["latency_sum_ms"] / m["invocations"]) if m["invocations"] else 0
        m["tpot_avg"] = round(m["tpot_sum"] / m["tpot_count"] / 1000, 2) if m["tpot_count"] else 0

    return sorted(models.values(), key=lambda x: x["cost_usd"], reverse=True)


def get_by_caller(account_region: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Get usage grouped by caller."""
    items = query_usage(account_region, _plan_segments(start_dt, end_dt), "CALLER#")

    callers = {}
    for item in items:
        caller = item["dimension"].replace("CALLER#", "")
        if caller not in callers:
            callers[caller] = {"caller": caller, "invocations": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cost_input": 0.0, "cost_output": 0.0, "cost_cache_read": 0.0, "cost_cache_write": 0.0}
        for k in ("invocations", "input_tokens", "output_tokens"):
            callers[caller][k] += item.get(k, 0)
        for k in ("cost_usd", "cost_input", "cost_output", "cost_cache_read", "cost_cache_write"):
            callers[caller][k] += item.get(k, 0.0)

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
    items = query_usage(account_region, _plan_segments(start_dt, end_dt), dimension)
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
                "cost_cache_read", "cost_cache_write", "latency_sum_ms",
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


def get_all_pricing() -> list[dict]:
    """Get current effective price for all models."""
    # Scan all MODEL# items, then pick latest SK per model
    resp = _pricing.scan()
    items = resp.get("Items", [])
    while resp.get("LastEvaluatedKey"):
        resp = _pricing.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    # Group by PK, pick latest SK (= current effective price)
    models: dict[str, dict] = {}
    for item in items:
        pk = item["PK"]
        if not pk.startswith("MODEL#"):
            continue
        if pk not in models or item["SK"] > models[pk]["SK"]:
            models[pk] = item

    result = []
    for pk, item in sorted(models.items()):
        model_id = pk.replace("MODEL#", "")
        result.append({
            "model_id": model_id,
            "input_per_1k": float(item.get("input_per_1k", 0)),
            "output_per_1k": float(item.get("output_per_1k", 0)),
            "effective_date": item["SK"],
            "source": item.get("source", ""),
        })
    return result


def get_pricing_sync_info() -> dict | None:
    """Get last pricing sync metadata."""
    try:
        resp = _usage.get_item(Key={"PK": "META", "SK": "PRICING_SYNC#latest"})
        return resp.get("Item")
    except Exception:
        return None


_cw = boto3.client("cloudwatch", region_name=AWS_REGION)


def get_ttft_trend(model_id: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Get TimeToFirstToken trend from CloudWatch for a model."""
    span_days = (end_dt - start_dt).total_seconds() / 86400
    period = 3600 if span_days <= 7 else 86400
    try:
        resp = _cw.get_metric_data(
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
    return [{"period": t.strftime("%Y-%m-%dT%H" if period == 3600 else "%Y-%m-%d"),
             "ttft_avg": round(results.get("avg", {}).get(t, 0)),
             "ttft_p99": round(results.get("p99", {}).get(t, 0))} for t in timestamps]


def save_pricing(model_id: str, input_per_1k: float, output_per_1k: float, effective_date: str):
    """Save a manual pricing record."""
    _pricing.put_item(Item={
        "PK": f"MODEL#{model_id}",
        "SK": effective_date,
        "input_per_1k": str(round(input_per_1k, 6)),
        "output_per_1k": str(round(output_per_1k, 6)),
        "source": "manual",
    })


def get_pricing_history(model_id: str) -> list[dict]:
    """Get all pricing records for a model, newest first."""
    resp = _pricing.query(
        KeyConditionExpression=Key("PK").eq(f"MODEL#{model_id}"),
        ScanIndexForward=False,
    )
    return [{
        "model_id": model_id,
        "input_per_1k": float(item.get("input_per_1k", 0)),
        "output_per_1k": float(item.get("output_per_1k", 0)),
        "effective_date": item["SK"],
        "source": item.get("source", ""),
    } for item in resp.get("Items", [])]


def delete_pricing(model_id: str, effective_date: str):
    """Delete a pricing record."""
    _pricing.delete_item(Key={"PK": f"MODEL#{model_id}", "SK": effective_date})


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
