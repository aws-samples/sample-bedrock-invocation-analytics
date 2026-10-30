# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda L2: cost aggregation by recomputing whole periods from Iceberg.

Every write recomputes complete HOURLY / DAILY / MONTHLY rows from the deduplicated event log
(usage_events is append-only, so one row per request_id is selected in SQL) and replaces the rows of
that period. Rerunning any step is harmless. All writers hold the lease lock META/L2#lock.

Entry points (event payload):
  {}                                    incremental: hours touched by newly ingested events (every few minutes),
                                        then one reprice chunk; {"incremental_only": true} skips the chunk
  {"settle": {}}                        dirty days, the last 2 completed days, catch-up after SETTLED (hourly)
  {"rebuild": {"start", "end", "dry_run", "resume"}}  explicit range, resumable per month; returns
                                        {"next": first day of the month to resume at} until done
The scheduled incremental run also advances a WebUI reprice job by one chunk (reprice.py).
"""

import csv
import io
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3

import pricing

STACK_NAME = os.environ.get("STACK_NAME", "BedrockInvocationAnalytics")
USAGE_STATS_TABLE = os.environ.get("USAGE_STATS_TABLE", "")
PRICING_TABLE = os.environ.get("MODEL_PRICING_TABLE", "")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "")
ICEBERG_CATALOG = os.environ.get("ICEBERG_CATALOG", "")
ICEBERG_DATABASE = os.environ.get("ICEBERG_DATABASE", "bedrock_analytics")
ICEBERG_TABLE = os.environ.get("ICEBERG_TABLE", "usage_events")

OVERLAP = timedelta(minutes=15)       # > measured max visibility lag (263 s) with margin
INGEST_SETTLE = timedelta(seconds=60)
MAX_WINDOW = timedelta(hours=6)
INITIAL_LOOKBACK = timedelta(hours=1)
HOURLY_TTL = timedelta(days=90)
DAILY_TTL = timedelta(days=365)
RECENT_COMPLETED_DAYS = 2
MAX_CATCHUP_DAYS = 31
COUNT_CHECK_DAYS = 90
LOCK_WAIT_S = 120
STOP_MARGIN_S = 60
MONTH_MARGIN_S = 300  # a month needs two Athena queries (each ≤ 120 s) before its first write

SUM_FIELDS = ("invocations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "cost_micro_usd", "cost_input_micro", "cost_output_micro", "cost_cache_read_micro",
              "cost_cache_write_micro", "cost_request_micro", "latency_sum_ms", "tpot_sum", "tpot_count")
MAX_FIELDS = ("max_latency_ms", "tpot_max")
MIN_FIELDS = ("min_latency_ms", "tpot_min")
VALUE_FIELDS = SUM_FIELDS + MAX_FIELDS + MIN_FIELDS
# Billed per model unit and time, not per token (parse_log keeps these ARN resource types): counted, never "unpriced".
NOT_TOKEN_BILLED = ("imported-model/", "provisioned-model/")

_clients: dict = {}


def client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Lease lock ──

class LockBusy(Exception):
    pass


class Lease:
    """META/L2#lock held for the rest of this invocation (+30 s). Expired leases can be taken over."""

    def __init__(self, context, wait_s: int = 0):
        self.owner, self.context, self.wait_s = str(uuid.uuid4()), context, wait_s

    def __enter__(self):
        deadline = time.time() + self.wait_s
        while True:
            now = int(time.time())
            expires = now + remaining_s(self.context) + 30
            try:
                client("dynamodb").put_item(
                    TableName=USAGE_STATS_TABLE,
                    Item={"PK": {"S": "META"}, "SK": {"S": "L2#lock"}, "owner": {"S": self.owner},
                          "expires_at": {"N": str(expires)}, "ttl": {"N": str(expires + 86400)}},
                    ConditionExpression="attribute_not_exists(PK) OR expires_at < :now",
                    ExpressionAttributeValues={":now": {"N": str(now)}})
                return self
            except client("dynamodb").exceptions.ConditionalCheckFailedException:
                if time.time() >= deadline:
                    raise LockBusy() from None
                time.sleep(5)

    def __exit__(self, *exc):
        try:
            client("dynamodb").delete_item(
                TableName=USAGE_STATS_TABLE, Key={"PK": {"S": "META"}, "SK": {"S": "L2#lock"}},
                ConditionExpression="#o = :me", ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":me": {"S": self.owner}})
        except client("dynamodb").exceptions.ConditionalCheckFailedException:
            print("Lock already taken over; not releasing")
        return False


def remaining_s(context) -> int:
    return int(context.get_remaining_time_in_millis() / 1000) if context else 300


# ── Entry point ──

def handler(event, context):
    now = utcnow()
    try:
        if "rebuild" in event:
            with Lease(context, wait_s=LOCK_WAIT_S):
                return run_rebuild(event["rebuild"], now, context)
        if "settle" in event:
            with Lease(context, wait_s=LOCK_WAIT_S):
                return run_settle(now, context)
        with Lease(context, wait_s=0):
            result = run_incremental(now, context)
            if not event.get("incremental_only"):  # rebuild_costs.sh between months: keep its own pace
                import reprice  # noqa: PLC0415  (reprice imports this module)
                result["reprice"] = reprice.run_chunk(context)  # raises only after the incremental work is committed
            return result
    except LockBusy:
        emit(now, {"LockBusy": 1})
        if "rebuild" in event:
            raise  # rebuild_costs.sh retries with backoff
        if "settle" in event:
            emit(now, {"SettleLagHours": max((hours_since_settled(pk, now) for pk in list_accounts()), default=0)})
        print("Lock busy; skipping this run")
        return {"skipped": "lock busy"}


def tick_handler(event, context):
    """WebUI-invokable entry point: always one incremental run (plus a reprice chunk); the payload is ignored."""
    return handler({}, context)


# ── Incremental ──

def run_incremental(now: datetime, context):
    checkpoint = get_meta("L2#latest") or {}
    watermark = parse_iso(checkpoint.get("last_window_end")) or (now - INITIAL_LOOKBACK)
    start = watermark - OVERLAP
    end = min((now - INGEST_SETTLE).replace(microsecond=0), watermark + MAX_WINDOW)
    end = max(end, watermark)

    touched = athena_rows(f"""
        SELECT DISTINCT account_id || '#' || region AS pk, date_format(ts, '%Y-%m-%dT%H') AS hour
        FROM {table_ref()} WHERE parsed_at >= {ts_lit(start)} AND parsed_at < {ts_lit(end)}""")
    current, previous = now.replace(minute=0, second=0, microsecond=0), now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    pks = {r["pk"] for r in touched} | set(list_accounts())
    hours = {(r["pk"], parse_hour(r["hour"])) for r in touched}
    hours |= {(pk, h) for pk in pks for h in (current, previous)}

    stats = recompute_hours(hours, now, context, dry_run=False)
    today = now.date()
    dirty = {(pk, h.date()) for pk, h in hours if h.date() < today}
    for pk, day in dirty:
        put_meta(f"DIRTY_DAY#{pk}#{day.isoformat()}", {"marked_at": now.isoformat()})
    set_checkpoint(end, now)
    register_accounts({pk for pk, _ in hours}, now)
    emit(now, {"CheckpointLagSeconds": int((now - end).total_seconds()), "HoursRecomputed": len(hours),
               "DirtyDays": len(dirty), "UnpricedEvents": stats["unpriced_events"],
               "DataScannedInBytes": stats["scanned_bytes"]})
    result = {"window_end": end.isoformat(), "hours": len(hours), "dirty_days": len(dirty), **stats}
    print(json.dumps(result, default=str))
    return result


# ── Settle and rebuild ──

def run_settle(now: datetime, context):
    yesterday = now.date() - timedelta(days=1)
    pks = set(list_accounts()) | iceberg_pks()
    days: dict[str, set[date]] = defaultdict(set)
    markers = {}
    for item in meta_items("DIRTY_DAY#"):
        pk, day = item["SK"].removeprefix("DIRTY_DAY#").rsplit("#", 1)
        days[pk].add(date.fromisoformat(day))
        markers[(pk, day)] = item.get("marked_at")
    check_due = count_check_due(now)
    if check_due:
        for pk, day in count_mismatches(now):
            marked_at = now.isoformat()
            put_meta(f"DIRTY_DAY#{pk}#{day.isoformat()}", {"marked_at": marked_at})
            days[pk].add(day)
            markers[(pk, day.isoformat())] = marked_at
    for pk in pks:
        settled = settled_day(pk, now)
        catch_up = [settled + timedelta(days=n) for n in range(1, MAX_CATCHUP_DAYS + 1)
                    if settled + timedelta(days=n) <= yesterday]
        days[pk] |= set(catch_up)
        days[pk] |= {yesterday - timedelta(days=n) for n in range(RECENT_COMPLETED_DAYS)}

    result = settle_days(days, now, context, dry_run=False, markers=markers)
    if check_due and result["next"] is None:
        put_meta("COUNT_CHECK#latest", {"day": now.date().isoformat()})
    lag = max((hours_since_settled(pk, now) for pk in pks), default=0)
    emit(now, {"SettleLagHours": lag, "DaysSettled": result["days"], "UnpricedEvents": result["unpriced_events"]})
    print(json.dumps(result, default=str))
    return result


def run_rebuild(spec: dict, now: datetime, context):
    first_full = iceberg_first_complete_days()
    if not first_full:
        return {"done": True, "reason": "no events"}
    yesterday = now.date() - timedelta(days=1)
    requested = date.fromisoformat(spec["start"]) if spec.get("start") not in (None, "", "all") else min(first_full.values())
    resume = date.fromisoformat(spec["resume"]) if spec.get("resume") else requested
    end = min(date.fromisoformat(spec["end"]) if spec.get("end") else yesterday, yesterday)
    if spec.get("start") == "all" and not spec.get("resume") and not spec.get("dry_run"):
        clear_legacy_unpriced()
    days = {}
    for pk, first in first_full.items():
        start = max(requested, first, resume)
        days[pk] = {start + timedelta(days=n) for n in range((end - start).days + 1)}
    result = settle_days(days, now, context, dry_run=bool(spec.get("dry_run")), markers={}, first_full=first_full)
    result["done"] = result["next"] is None
    print(json.dumps(result, default=str))
    return result


def settle_days(days: dict[str, set[date]], now: datetime, context, dry_run: bool, markers: dict,
                first_full: dict[str, date] | None = None) -> dict:
    """Recompute HOURLY (within TTL) + DAILY for the requested days, month by month, and MONTHLY for completed
    months touched. SETTLED and DIRTY_DAY markers advance per day in the current month and only after MONTHLY in
    completed months. Stops before the time budget runs out and reports the month to resume at."""
    by_month: dict[tuple[int, int], dict[str, set[date]]] = defaultdict(lambda: defaultdict(set))
    for pk, ds in days.items():
        for d in ds:
            if d < now.date():
                by_month[(d.year, d.month)][pk].add(d)
    totals = {"days": 0, "puts": 0, "deletes": 0, "unpriced_events": 0, "scanned_bytes": 0, "diffs": []}
    for (y, m) in sorted(by_month):
        month_start = date(y, m, 1)
        if remaining_s(context) < MONTH_MARGIN_S:
            return settle_result(totals, month_start)
        month_end = next_month(month_start)
        complete = month_end <= now.date()
        wanted = by_month[(y, m)]
        q_start = month_start if complete else min(min(v) for v in wanted.values())
        q_end = month_end if complete else max(max(v) for v in wanted.values()) + timedelta(days=1)
        hourly, unpriced, scanned = aggregate(day_start(q_start), day_start(q_end))
        totals["scanned_bytes"] += scanned
        totals["unpriced_events"] += sum(u["count"] for u in unpriced.values())
        daily = rollup(hourly, "DAILY", lambda h: h[:10])
        deferred = []  # (pk, day, marker) committed only after the month's MONTHLY rows are written
        for pk in sorted(wanted):
            for d in sorted(wanted[pk]):
                if remaining_s(context) < STOP_MARGIN_S:
                    return settle_result(totals, month_start)  # redone next time; writes are diffs, so cheap
                write_day(pk, d, hourly, daily, now, dry_run, totals)
                if dry_run:
                    continue
                marker_at = markers.get((pk, d.isoformat()))
                if complete:
                    deferred.append((pk, d, marker_at))
                else:
                    advance_settled(pk, d, now)
                    if marker_at is not None:
                        delete_marker(pk, d, marker_at)
        if complete:
            monthly = rollup(hourly, "MONTHLY", lambda h: h[:7])
            for pk in sorted(wanted):
                if first_full and first_full.get(pk, month_start) > month_start:
                    continue  # Iceberg covers this month only partly; keep the existing MONTHLY row
                p, dl = replace_scope(pk, f"MONTHLY#{y:04d}-{m:02d}#", monthly.get(pk, {}), ttl=None,
                                      dry_run=dry_run, diffs=totals["diffs"])
                totals["puts"] += p; totals["deletes"] += dl
            for pk, d, marker_at in deferred:
                advance_settled(pk, d, now)
                if marker_at is not None:
                    delete_marker(pk, d, marker_at)
        if not dry_run:
            scope_days = {q_start + timedelta(days=n) for n in range((q_end - q_start).days)} - {now.date()}
            # days before a pk's first full Iceberg day predate the table and are never rewritten
            fully_rewritten = all({d for d in scope_days if not first_full or d >= first_full.get(pk, d)} <= wanted.get(pk, set())
                                  for pk in set(wanted) | set(hourly))
            if fully_rewritten:
                update_unpriced(unpriced, day_start(q_start), day_start(q_end), now)
            else:
                mark_unpriced(unpriced, now)  # only part of the scope was rewritten; never clear here
    return settle_result(totals, None)


def write_day(pk: str, d: date, hourly: dict, daily: dict, now: datetime, dry_run: bool, totals: dict):
    """Replace one pk-day: HOURLY rows for hours still within TTL, then the DAILY rows."""
    period_hours = {f"{d.isoformat()}T{h:02d}" for h in range(24)}
    hour_rows = {sk: v for sk, v in hourly.get(pk, {}).items() if sk.split("#")[1] in period_hours}
    day_rows = {sk: v for sk, v in daily.get(pk, {}).items() if sk.split("#")[1] == d.isoformat()}
    writable_hours = {h for h in period_hours if parse_hour(h) + HOURLY_TTL > now}
    current_hours = {i["SK"]: i for i in query_prefix(pk, f"HOURLY#{d.isoformat()}T")} if writable_hours else {}
    for h in sorted(writable_hours):
        p, dl = replace_scope(pk, f"HOURLY#{h}#", {sk: v for sk, v in hour_rows.items() if sk.split("#")[1] == h},
                              ttl=int((parse_hour(h) + HOURLY_TTL).timestamp()), dry_run=dry_run, existing=current_hours)
        totals["puts"] += p; totals["deletes"] += dl
    p, dl = replace_scope(pk, f"DAILY#{d.isoformat()}#", day_rows, ttl=int((now + DAILY_TTL).timestamp()),
                          dry_run=dry_run, diffs=totals["diffs"])
    totals["puts"] += p; totals["deletes"] += dl
    totals["days"] += 1


def trim_diffs(diffs: list[dict], limit: int = 300) -> list[dict]:
    """Periods whose invocations would drop first, then the largest cost changes; at most `limit` entries."""
    drops = [d for d in diffs if d["drop"]][:limit]
    others = sorted((d for d in diffs if not d["drop"]),
                    key=lambda d: -abs(int(d.get("cost_after", 0)) - int(d.get("cost_before", 0))))
    return drops + others[:limit - len(drops)]


def settle_result(totals: dict, next_month_start: date | None) -> dict:
    diffs = totals["diffs"]
    return {**totals, "diffs": trim_diffs(diffs), "next": next_month_start.isoformat() if next_month_start else None,
            "drops": sum(1 for d in diffs if d["drop"]),
            "days_changed": sum(1 for d in diffs if len(d["period"]) == 10),
            "months_changed": sum(1 for d in diffs if len(d["period"]) == 7)}


def recompute_hours(hours: set[tuple[str, datetime]], now: datetime, context, dry_run: bool,
                    clear_unpriced: bool = False) -> dict:
    """Recompute the given (pk, hour) HOURLY rows; hours are grouped into one query per month.
    clear_unpriced: the hours cover every pk over [first, last hour] of each month, so markers inside that span clear."""
    stats = {"puts": 0, "deletes": 0, "unpriced_events": 0, "scanned_bytes": 0}
    by_month = defaultdict(set)
    for pk, h in hours:
        by_month[(h.year, h.month)].add((pk, h))
    for key in sorted(by_month):
        group = by_month[key]
        lo, hi = min(h for _, h in group), max(h for _, h in group) + timedelta(hours=1)
        hourly, unpriced, scanned = aggregate(lo, hi)
        stats["scanned_bytes"] += scanned
        stats["unpriced_events"] += sum(u["count"] for u in unpriced.values())
        for pk, h in sorted(group):
            if h + HOURLY_TTL <= now:
                continue
            label = h.strftime("%Y-%m-%dT%H")
            rows = {sk: v for sk, v in hourly.get(pk, {}).items() if sk.split("#")[1] == label}
            p, d = replace_scope(pk, f"HOURLY#{label}#", rows, ttl=int((h + HOURLY_TTL).timestamp()), dry_run=dry_run)
            stats["puts"] += p; stats["deletes"] += d
        if dry_run:
            continue
        if clear_unpriced:
            update_unpriced(unpriced, lo, hi, now)
        else:
            mark_unpriced(unpriced, now)
    return stats


# ── Aggregation in Athena ──

def aggregate(ts_from: datetime, ts_to: datetime):
    """HOURLY rows {pk: {sk: values}} for events with ts in [ts_from, ts_to), plus unpriced counts per model."""
    models = {r["model_id"] for r in athena_rows(
        f"SELECT DISTINCT coalesce(model_id, 'unknown') AS model_id FROM {table_ref()} "
        f"WHERE ts >= {ts_lit(ts_from)} AND ts < {ts_lit(ts_to)} AND error_code IS NULL")}
    versions = pricing.load_versions(boto3.resource("dynamodb").Table(PRICING_TABLE), models)
    rows, scanned = athena_rows(aggregate_sql(ts_from, ts_to, pricing.values_table(versions)), with_scanned=True)
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    unpriced: dict[str, dict] = {}
    for r in rows:
        kind = r["kind"]
        if kind == "CALLER" and not r["caller"]:
            continue
        dim = {"MODEL": f"MODEL#{r['model_id']}", "CALLER": f"CALLER#{r['caller']}", "TOTAL": "TOTAL"}[kind]
        values = {f: int(r[f] or 0) for f in VALUE_FIELDS}
        out[r["pk"]][f"HOURLY#{r['hour']}#{dim}"] = {k: v for k, v in values.items() if v or k in SUM_FIELDS}
        if kind == "MODEL" and int(r["unpriced"] or 0) and not r["model_id"].startswith(NOT_TOKEN_BILLED):
            span = unpriced.setdefault(r["model_id"], {"count": 0, "first": None, "last": None, "accounts": set()})
            span["accounts"].add(r["pk"])
            first, last = athena_ts(r["unpriced_first"]), athena_ts(r["unpriced_last"])
            span["count"] += int(r["unpriced"])
            span["first"] = min(filter(None, (span["first"], first)))
            span["last"] = max(filter(None, (span["last"], last)))
    return out, unpriced, scanned


def athena_ts(value: str | None) -> datetime | None:
    """Athena timestamp text 'YYYY-MM-DD HH:MM:SS[.fff] UTC' → aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value[:19].replace(" ", "T")).replace(tzinfo=timezone.utc)


def aggregate_sql(ts_from: datetime, ts_to: datetime, values: str) -> str:
    cost = lambda col, p: f"(CAST({col} AS BIGINT) * coalesce({p}, 0) + 500) / 1000"  # noqa: E731
    return f"""
WITH ranked AS (
  SELECT *, row_number() OVER (PARTITION BY request_id ORDER BY parsed_at) AS rn
  FROM {table_ref()} WHERE ts >= {ts_lit(ts_from)} AND ts < {ts_lit(ts_to)}
),
e AS (
  SELECT account_id || '#' || region AS pk, date_format(ts, '%Y-%m-%dT%H') AS hour, ts,
         coalesce(model_id, 'unknown') AS model_id, coalesce(caller, '') AS caller,
         coalesce(input_tokens, 0) AS inp, coalesce(output_tokens, 0) AS outp, coalesce(cache_read_tokens, 0) AS cr,
         coalesce(cache_write_total_tokens, 0) AS cw_total,
         CASE WHEN cache_write_5m_tokens IS NULL AND cache_write_1h_tokens IS NULL THEN coalesce(cache_write_total_tokens, 0)
              ELSE coalesce(cache_write_5m_tokens, 0) END AS cw5,
         CASE WHEN cache_write_5m_tokens IS NULL AND cache_write_1h_tokens IS NULL THEN 0
              ELSE coalesce(cache_write_1h_tokens, 0) END AS cw1,
         CAST(coalesce(latency_ms, 0) AS BIGINT) AS lat
  FROM ranked WHERE rn = 1 AND error_code IS NULL
),
prices (pm, eff_from, eff_to, p_in, p_out, p_cr, p_cw, p_cw1h, p_req) AS (VALUES
{values}
),
c AS (
  SELECT e.*, p.pm IS NULL AS unpriced_flag,
         {cost('inp', 'p_in')} AS c_in, {cost('outp', 'p_out')} AS c_out, {cost('cr', 'p_cr')} AS c_cr,
         {cost('cw5', 'p_cw')} + {cost('cw1', 'p_cw1h')} AS c_cw, coalesce(p_req, 0) AS c_req,
         CASE WHEN outp > 0 AND lat > 0 THEN (lat * 2000 + outp) / (2 * outp) END AS tpot
  FROM e LEFT JOIN prices p ON p.pm = e.model_id AND e.ts >= p.eff_from AND e.ts < p.eff_to
)
SELECT pk, hour, model_id, caller,
       CASE WHEN grouping(model_id) = 0 THEN 'MODEL' WHEN grouping(caller) = 0 THEN 'CALLER' ELSE 'TOTAL' END AS kind,
       count(*) AS invocations, sum(inp) AS input_tokens, sum(outp) AS output_tokens, sum(cr) AS cache_read_tokens,
       sum(cw_total) AS cache_write_tokens,
       sum(c_in + c_out + c_cr + c_cw + c_req) AS cost_micro_usd, sum(c_in) AS cost_input_micro, sum(c_out) AS cost_output_micro,
       sum(c_cr) AS cost_cache_read_micro, sum(c_cw) AS cost_cache_write_micro, sum(c_req) AS cost_request_micro,
       sum(lat) AS latency_sum_ms, coalesce(sum(tpot), 0) AS tpot_sum, count(tpot) AS tpot_count,
       coalesce(max(CASE WHEN lat > 0 THEN lat END), 0) AS max_latency_ms,
       coalesce(min(CASE WHEN lat > 0 THEN lat END), 0) AS min_latency_ms,
       coalesce(max(tpot), 0) AS tpot_max, coalesce(min(tpot), 0) AS tpot_min,
       count_if(unpriced_flag) AS unpriced,
       CAST(min(CASE WHEN unpriced_flag THEN ts END) AS VARCHAR) AS unpriced_first,
       CAST(max(CASE WHEN unpriced_flag THEN ts END) AS VARCHAR) AS unpriced_last
FROM c GROUP BY GROUPING SETS ((pk, hour, model_id), (pk, hour, caller), (pk, hour))
"""


def rollup(hourly: dict[str, dict[str, dict]], granularity: str, period_of) -> dict[str, dict[str, dict]]:
    """Merge HOURLY rows into DAILY or MONTHLY rows: sums add, max/min merge (0 means no value)."""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for pk, rows in hourly.items():
        for sk, values in rows.items():
            _, hour, dim = sk.split("#", 2)
            target = out[pk].setdefault(f"{granularity}#{period_of(hour)}#{dim}", {f: 0 for f in SUM_FIELDS})
            merge(target, values)
    return out


def merge(target: dict, values: dict):
    for f in SUM_FIELDS:
        target[f] = target.get(f, 0) + values.get(f, 0)
    for f in MAX_FIELDS:
        if values.get(f):
            target[f] = max(target.get(f, 0), values[f])
    for f in MIN_FIELDS:
        if values.get(f):
            target[f] = values[f] if not target.get(f) else min(target[f], values[f])


# ── DynamoDB writes ──

def replace_scope(pk: str, prefix: str, rows: dict[str, dict], ttl: int | None, dry_run: bool, diffs: list | None = None,
                  existing: dict[str, dict] | None = None):
    """Make the rows under (pk, prefix) equal `rows`: put changed or new rows, then delete vanished ones.
    `existing` may carry the current rows of the scope when the caller already queried a wider prefix."""
    if existing is None:
        existing = {i["SK"]: i for i in query_prefix(pk, prefix)}
    else:
        existing = {sk: v for sk, v in existing.items() if sk.startswith(prefix)}
    puts = [(sk, v) for sk, v in rows.items() if not same_values(existing.get(sk), v)]
    deletes = [sk for sk in existing if sk not in rows]
    if diffs is not None and (puts or deletes) and prefix.startswith(("DAILY#", "MONTHLY#")):
        old = int((existing.get(prefix + "TOTAL") or {}).get("invocations", 0))
        new = int((rows.get(prefix + "TOTAL") or {}).get("invocations", 0))
        cost = lambda rows: int((rows.get(prefix + "TOTAL") or {}).get("cost_micro_usd", 0))  # noqa: E731
        diffs.append({"pk": pk, "period": prefix.split("#")[1], "invocations_before": old, "invocations_after": new,
                      "cost_before": cost(existing), "cost_after": cost(rows),
                      "puts": len(puts), "deletes": len(deletes), "drop": new < old})
    if dry_run:
        return len(puts), len(deletes)
    table = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE)
    computed_at = utcnow().isoformat()
    with table.batch_writer() as batch:
        for sk, values in puts:
            item = {"PK": pk, "SK": sk, "computed_at": computed_at, **values}
            if ttl is not None:
                item["ttl"] = ttl
            batch.put_item(Item=item)
    with table.batch_writer() as batch:
        for sk in deletes:
            batch.delete_item(Key={"PK": pk, "SK": sk})
    return len(puts), len(deletes)


def same_values(item: dict | None, values: dict) -> bool:
    if item is None:
        return False
    return all(int(item.get(f, 0)) == int(values.get(f, 0)) for f in VALUE_FIELDS)


def query_prefix(pk: str, prefix: str):
    table = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE)
    kwargs = {"KeyConditionExpression": "PK = :pk AND begins_with(SK, :p)",
              "ExpressionAttributeValues": {":pk": pk, ":p": prefix}}
    while True:
        resp = table.query(**kwargs)
        yield from resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            return
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ── META items ──

def get_meta(sk: str) -> dict | None:
    item = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).get_item(Key={"PK": "META", "SK": sk}).get("Item")
    return {k: (int(v) if isinstance(v, Decimal) else v) for k, v in item.items()} if item else None


def put_meta(sk: str, attrs: dict):
    boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).put_item(Item={"PK": "META", "SK": sk, **attrs})


def meta_items(prefix: str) -> list[dict]:
    return list(query_prefix("META", prefix))


def list_accounts() -> list[str]:
    return [i["SK"].removeprefix("ACCOUNT#") for i in meta_items("ACCOUNT#")]


def register_accounts(pks: set[str], now: datetime):
    table = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE)
    for pk in pks:
        table.update_item(Key={"PK": "META", "SK": f"ACCOUNT#{pk}"},
                          UpdateExpression="SET registered_at = if_not_exists(registered_at, :n), last_seen = :n",
                          ExpressionAttributeValues={":n": now.isoformat()})


def set_checkpoint(window_end: datetime, now: datetime):
    """Forward-only, so a slower concurrent run can never move it back."""
    try:
        boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).put_item(
            Item={"PK": "META", "SK": "L2#latest", "last_window_end": window_end.isoformat(), "processed_at": now.isoformat()},
            ConditionExpression="attribute_not_exists(last_window_end) OR last_window_end <= :w",
            ExpressionAttributeValues={":w": window_end.isoformat()})
    except client("dynamodb").exceptions.ConditionalCheckFailedException:
        print("Checkpoint already ahead")


def settled_day(pk: str, now: datetime) -> date:
    """META/SETTLED#pk: last day of the contiguous settled prefix. Initialized once (attribute_not_exists) to the last
    day that already has a DAILY TOTAL row; a pk without DAILY rows starts the day before its first Iceberg event."""
    item = get_meta(f"SETTLED#{pk}")
    if item:
        return date.fromisoformat(item["day"])
    last = [i["SK"].split("#")[1] for i in query_prefix(pk, "DAILY#") if i["SK"].endswith("#TOTAL")]
    if last:
        initial = date.fromisoformat(max(last))
    else:
        first = iceberg_first_day(pk)
        initial = (first - timedelta(days=1)) if first else (now.date() - timedelta(days=1))
    try:
        boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).put_item(
            Item={"PK": "META", "SK": f"SETTLED#{pk}", "day": initial.isoformat()},
            ConditionExpression="attribute_not_exists(PK)")
    except client("dynamodb").exceptions.ConditionalCheckFailedException:
        return date.fromisoformat(get_meta(f"SETTLED#{pk}")["day"])
    return initial


def advance_settled(pk: str, day: date, now: datetime):
    """Extend the contiguous settled prefix by one day when `day` directly follows it."""
    current = settled_day(pk, now)
    if day != current + timedelta(days=1):
        return
    try:
        boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).put_item(
            Item={"PK": "META", "SK": f"SETTLED#{pk}", "day": day.isoformat()},
            ConditionExpression="#d = :cur", ExpressionAttributeNames={"#d": "day"},
            ExpressionAttributeValues={":cur": current.isoformat()})
    except client("dynamodb").exceptions.ConditionalCheckFailedException:
        pass


def hours_since_settled(pk: str, now: datetime) -> int:
    item = get_meta(f"SETTLED#{pk}")
    if not item:
        return 0
    end = day_start(date.fromisoformat(item["day"]) + timedelta(days=1))
    return int((now - end).total_seconds() // 3600)


def delete_marker(pk: str, day: date, marked_at: str):
    """Remove a DIRTY_DAY marker only if no newer event re-marked it while the day was being recomputed."""
    try:
        boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).delete_item(
            Key={"PK": "META", "SK": f"DIRTY_DAY#{pk}#{day.isoformat()}"},
            ConditionExpression="marked_at = :m", ExpressionAttributeValues={":m": marked_at})
    except client("dynamodb").exceptions.ConditionalCheckFailedException:
        pass


def mark_unpriced(unpriced: dict[str, dict], now: datetime):
    """META/UNPRICED#model holds the event-time span [first_seen, last_seen] of events counted at $0."""
    table = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE)
    for model, span in unpriced.items():
        current = get_meta(f"UNPRICED#{model}") or {}
        first = min(filter(None, (parse_iso(current.get("first_seen")), span["first"])))
        last = max(filter(None, (parse_iso(current.get("last_seen")), span["last"])))
        accounts = set(current.get("accounts") or ()) | set(span.get("accounts") or ())  # pk = account#region
        table.put_item(Item={"PK": "META", "SK": f"UNPRICED#{model}", "first_seen": first.isoformat(),
                             "last_seen": last.isoformat(), "updated_at": now.isoformat(),
                             **({"accounts": accounts} if accounts else {})})


def clear_legacy_unpriced():
    """Markers from before event-time spans can't be cleared by span; a full rebuild re-marks what is still unpriced."""
    for item in meta_items("UNPRICED#"):
        if not item.get("first_seen"):
            boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).delete_item(Key={"PK": "META", "SK": item["SK"]})


def update_unpriced(unpriced: dict[str, dict], scope_from: datetime, scope_to: datetime, now: datetime):
    """Mark models still unpriced; clear a marker only if this recompute covered its whole event-time span."""
    mark_unpriced(unpriced, now)
    table = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE)
    for item in meta_items("UNPRICED#"):
        model = item["SK"].removeprefix("UNPRICED#")
        if model in unpriced:
            continue
        first, last = parse_iso(item.get("first_seen")), parse_iso(item.get("last_seen"))
        if not (first and last) or not (scope_from <= first < scope_to):
            continue
        if last < scope_to:
            table.delete_item(Key={"PK": "META", "SK": item["SK"]})
        else:  # this scope priced the leading part of the span; the rest stays flagged
            table.update_item(Key={"PK": "META", "SK": item["SK"]}, UpdateExpression="SET first_seen = :f",
                              ExpressionAttributeValues={":f": scope_to.isoformat()})


# ── Count check (catches events that became visible after their period settled) ──

def count_check_due(now: datetime) -> bool:
    last = (get_meta("COUNT_CHECK#latest") or {}).get("day")
    return last != now.date().isoformat()


def count_mismatches(now: datetime) -> list[tuple[str, date]]:
    start = now.date() - timedelta(days=COUNT_CHECK_DAYS)
    first_full = iceberg_first_complete_days()
    rows = athena_rows(f"""
        SELECT pk, day, count(*) AS n FROM (
          SELECT request_id, arbitrary(account_id || '#' || region) AS pk, arbitrary(date_format(ts, '%Y-%m-%d')) AS day
          FROM {table_ref()} WHERE ts >= {ts_lit(day_start(start))} AND ts < {ts_lit(day_start(now.date()))}
            AND error_code IS NULL GROUP BY request_id) GROUP BY pk, day""")
    out = []
    for r in rows:
        d = date.fromisoformat(r["day"])
        if d < first_full.get(r["pk"], d):
            continue  # Iceberg covers this day only partly; its DAILY row came from elsewhere
        item = boto3.resource("dynamodb").Table(USAGE_STATS_TABLE).get_item(
            Key={"PK": r["pk"], "SK": f"DAILY#{r['day']}#TOTAL"}).get("Item")
        if d <= settled_day(r["pk"], now) and int((item or {}).get("invocations", 0)) != int(r["n"]):
            out.append((r["pk"], d))
    return out


# ── Iceberg helpers ──

def iceberg_pks() -> set[str]:
    return {r["pk"] for r in athena_rows(f"SELECT DISTINCT account_id || '#' || region AS pk FROM {table_ref()}")}


def iceberg_first_day(pk: str) -> date | None:
    acct, region = pk.split("#", 1)
    rows = athena_rows(f"SELECT date_format(min(ts), '%Y-%m-%d') AS d FROM {table_ref()} "
                       f"WHERE account_id = '{sql_str(acct)}' AND region = '{sql_str(region)}'")
    return date.fromisoformat(rows[0]["d"]) if rows and rows[0]["d"] else None


def iceberg_first_complete_days() -> dict[str, date]:
    """First UTC day fully covered by Iceberg, per pk (a day starting mid-way was partly recorded elsewhere)."""
    out = {}
    for r in athena_rows(f"SELECT account_id || '#' || region AS pk, CAST(min(ts) AS VARCHAR) AS t FROM {table_ref()} GROUP BY 1"):
        first = athena_ts(r["t"])
        out[r["pk"]] = first.date() if first == day_start(first.date()) else first.date() + timedelta(days=1)
    return out


def athena_rows(sql: str, with_scanned: bool = False, timeout_s: int = 120):
    """Run a query (result reuse off) and read the CSV result from S3."""
    qid = client("athena").start_query_execution(
        QueryString=sql, WorkGroup=ATHENA_WORKGROUP,
        ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {"Enabled": False}})["QueryExecutionId"]
    deadline = time.time() + timeout_s
    while True:
        q = client("athena").get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = q["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Athena query {qid} {state}: {q['Status'].get('StateChangeReason', '')}")
        if time.time() > deadline:
            client("athena").stop_query_execution(QueryExecutionId=qid)
            raise TimeoutError(f"Athena query {qid} exceeded {timeout_s}s")
        time.sleep(0.5)
    bucket, key = q["ResultConfiguration"]["OutputLocation"].removeprefix("s3://").split("/", 1)
    body = client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(body)))
    if with_scanned:
        return rows, int(q.get("Statistics", {}).get("DataScannedInBytes", 0))
    return rows


def table_ref() -> str:
    return f'"{ICEBERG_CATALOG}"."{ICEBERG_DATABASE}"."{ICEBERG_TABLE}"'


# ── Small helpers ──

def emit(now: datetime, metrics: dict):
    """CloudWatch Embedded Metric Format, namespace BedrockAnalytics, dimension Stack."""
    print(json.dumps({"_aws": {"Timestamp": int(now.timestamp() * 1000), "CloudWatchMetrics": [{
        "Namespace": "BedrockAnalytics", "Dimensions": [["Stack"]],
        "Metrics": [{"Name": k} for k in metrics]}]}, "Stack": STACK_NAME, **metrics}))


def ts_lit(dt: datetime) -> str:
    return f"from_iso8601_timestamp('{dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}')"


def sql_str(value: str) -> str:
    return value.replace("'", "''")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def parse_hour(label: str) -> datetime:
    return datetime.strptime(label, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)


def day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
