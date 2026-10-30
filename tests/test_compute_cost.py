from datetime import date, datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

import compute_cost as cc
import pricing

UTC = timezone.utc
TABLE = "usage-stats-test"


# ── pricing ──

def item(sk, **prices):
    return {"SK": sk, **{k: str(v) for k, v in prices.items()}}


def test_versions_are_contiguous_and_bisected_by_timestamp():
    vs = pricing.versions_from_items([
        item("2026-10-01T12:00:00Z", input_per_1k=0.002),
        item("2000-01-01T00:00:00Z", input_per_1k=0.001, cache_write_per_1k=0.003),
    ])
    assert [v.eff_from.year for v in vs] == [2000, 2026] and vs[0].eff_to == vs[1].eff_from
    assert vs[0].prices[3] == vs[0].prices[4] == 3000  # 1h cache write falls back to 5m
    before = pricing.price_at(vs, datetime(2026, 10, 1, 11, 59, 59, tzinfo=UTC))
    at = pricing.price_at(vs, datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC))
    assert before[0] == 1000 and at[0] == 2000  # intraday change applies from the exact second
    assert pricing.price_at(vs, datetime(1999, 1, 1, tzinfo=UTC)) is None
    assert pricing.price_at(None, datetime(2026, 1, 1, tzinfo=UTC)) is None


def test_values_table_quotes_models_and_handles_empty():
    vs = {"o'model": pricing.versions_from_items([item("2000-01-01T00:00:00Z", input_per_1k=1)])}
    assert "'o''model'" in pricing.values_table(vs)
    assert "CAST(NULL AS VARCHAR)" in pricing.values_table({})


def test_aggregate_sql_dedups_casts_latency_and_rounds_half_up():
    sql = cc.aggregate_sql(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC), pricing.values_table({}))
    assert "row_number() OVER (PARTITION BY request_id ORDER BY parsed_at)" in sql and "rn = 1" in sql
    assert "CAST(coalesce(latency_ms, 0) AS BIGINT)" in sql
    assert "(lat * 2000 + outp) / (2 * outp)" in sql and "+ 500) / 1000" in sql
    assert "e.ts >= p.eff_from AND e.ts < p.eff_to" in sql
    assert "GROUPING SETS ((pk, hour, model_id), (pk, hour, caller), (pk, hour))" in sql


# ── rollup / merge ──

def row(**kw):
    base = {f: 0 for f in cc.SUM_FIELDS}
    base.update(kw)
    return base


def test_rollup_sums_and_merges_extremes_ignoring_zero():
    hourly = {"pk": {
        "HOURLY#2026-10-01T00#TOTAL": row(invocations=2, cost_micro_usd=10, max_latency_ms=50, min_latency_ms=5, tpot_min=3),
        "HOURLY#2026-10-01T05#TOTAL": row(invocations=1, cost_micro_usd=7, max_latency_ms=90, min_latency_ms=9),
        "HOURLY#2026-10-02T00#MODEL#m": row(invocations=4),
    }}
    daily = cc.rollup(hourly, "DAILY", lambda h: h[:10])["pk"]
    total = daily["DAILY#2026-10-01#TOTAL"]
    assert total["invocations"] == 3 and total["cost_micro_usd"] == 17
    assert total["max_latency_ms"] == 90 and total["min_latency_ms"] == 5 and total["tpot_min"] == 3
    monthly = cc.rollup(hourly, "MONTHLY", lambda h: h[:7])["pk"]
    assert monthly["MONTHLY#2026-10#MODEL#m"]["invocations"] == 4


# ── DynamoDB-backed behaviour ──

class Ctx:
    def __init__(self, seconds=300):
        self.seconds = seconds

    def get_remaining_time_in_millis(self):
        return self.seconds * 1000


@pytest.fixture
def ddb(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        boto3.client("dynamodb").create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}])
        monkeypatch.setattr(cc, "USAGE_STATS_TABLE", TABLE)
        cc._clients.clear()
        yield boto3.resource("dynamodb").Table(TABLE)
        cc._clients.clear()


def keys(table, pk):
    return sorted(i["SK"] for i in table.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": pk})["Items"])


def test_replace_scope_puts_changed_deletes_vanished_and_is_idempotent(ddb):
    ddb.put_item(Item={"PK": "pk", "SK": "DAILY#2026-10-01#MODEL#gone", "invocations": 1})
    ddb.put_item(Item={"PK": "pk", "SK": "DAILY#2026-10-01#TOTAL", "invocations": 1})
    ddb.put_item(Item={"PK": "pk", "SK": "DAILY#2026-10-02#TOTAL", "invocations": 9})  # other period untouched
    rows = {"DAILY#2026-10-01#TOTAL": row(invocations=3), "DAILY#2026-10-01#MODEL#m": row(invocations=3)}
    assert cc.replace_scope("pk", "DAILY#2026-10-01#", rows, ttl=None, dry_run=False) == (2, 1)
    assert keys(ddb, "pk") == ["DAILY#2026-10-01#MODEL#m", "DAILY#2026-10-01#TOTAL", "DAILY#2026-10-02#TOTAL"]
    assert cc.replace_scope("pk", "DAILY#2026-10-01#", rows, ttl=None, dry_run=False) == (0, 0)


def test_replace_scope_dry_run_reports_drop_without_writing(ddb):
    ddb.put_item(Item={"PK": "pk", "SK": "DAILY#2026-10-01#TOTAL", "invocations": 5})
    diffs = []
    cc.replace_scope("pk", "DAILY#2026-10-01#", {"DAILY#2026-10-01#TOTAL": row(invocations=4)}, ttl=None, dry_run=True, diffs=diffs)
    assert diffs[0]["drop"] and diffs[0]["invocations_before"] == 5 and diffs[0]["invocations_after"] == 4
    assert int(ddb.get_item(Key={"PK": "pk", "SK": "DAILY#2026-10-01#TOTAL"})["Item"]["invocations"]) == 5


def test_lease_is_exclusive_owner_released_and_taken_over_after_expiry(ddb, monkeypatch):
    with cc.Lease(Ctx(), wait_s=0):
        with pytest.raises(cc.LockBusy):
            cc.Lease(Ctx(), wait_s=0).__enter__()
    with cc.Lease(Ctx(), wait_s=0):
        pass  # released by the first holder
    stale = cc.Lease(Ctx(seconds=-60), wait_s=0).__enter__()  # expires in the past
    fresh = cc.Lease(Ctx(), wait_s=0).__enter__()
    stale.__exit__(None, None, None)  # must not delete the new holder's lock
    assert ddb.get_item(Key={"PK": "META", "SK": "L2#lock"})["Item"]["owner"] == fresh.owner


def test_settled_initializes_from_last_daily_then_advances_contiguously(ddb):
    now = datetime(2026, 10, 30, 3, tzinfo=UTC)
    ddb.put_item(Item={"PK": "pk", "SK": "DAILY#2026-10-27#TOTAL", "invocations": 1})
    assert cc.settled_day("pk", now) == date(2026, 10, 27)
    cc.advance_settled("pk", date(2026, 10, 29), now)  # gap: ignored
    assert cc.settled_day("pk", now) == date(2026, 10, 27)
    cc.advance_settled("pk", date(2026, 10, 28), now)
    assert cc.settled_day("pk", now) == date(2026, 10, 28)


def test_settled_for_new_pk_starts_before_first_event(ddb, monkeypatch):
    monkeypatch.setattr(cc, "iceberg_first_day", lambda pk: date(2026, 5, 1))
    assert cc.settled_day("new", datetime(2026, 10, 30, tzinfo=UTC)) == date(2026, 4, 30)


def test_dirty_marker_survives_when_re_marked_during_recompute(ddb):
    cc.put_meta("DIRTY_DAY#pk#2026-10-01", {"marked_at": "t2"})
    cc.delete_marker("pk", date(2026, 10, 1), "t1")
    assert cc.get_meta("DIRTY_DAY#pk#2026-10-01")
    cc.delete_marker("pk", date(2026, 10, 1), "t2")
    assert cc.get_meta("DIRTY_DAY#pk#2026-10-01") is None


def test_unpriced_marker_tracks_event_time_and_clears_only_when_span_is_covered(ddb):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    june = {"m": {"count": 3, "first": datetime(2026, 6, 5, tzinfo=UTC), "last": datetime(2026, 6, 20, tzinfo=UTC)}}
    cc.mark_unpriced(june, now)
    cc.mark_unpriced({"m": {"count": 1, "first": datetime(2026, 6, 25, tzinfo=UTC), "last": datetime(2026, 6, 25, tzinfo=UTC)}}, now)
    marker = cc.get_meta("UNPRICED#m")
    assert marker["first_seen"].startswith("2026-06-05") and marker["last_seen"].startswith("2026-06-25")
    cc.update_unpriced({}, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC), now)
    assert cc.get_meta("UNPRICED#m")  # an October recompute says nothing about June
    cc.update_unpriced({}, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC), now)
    assert cc.get_meta("UNPRICED#m") is None


def test_checkpoint_never_moves_back(ddb):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    cc.set_checkpoint(now, now)
    cc.set_checkpoint(now - timedelta(minutes=5), now)
    assert cc.get_meta("L2#latest")["last_window_end"] == now.isoformat()


def fake_aggregate(events):
    """events: list of (pk, hour_label, model, invocations) → aggregate()-shaped result."""
    def run(ts_from, ts_to):
        out = {}
        for pk, hour, model, n in events:
            if not (ts_from <= cc.parse_hour(hour) < ts_to):
                continue
            rows = out.setdefault(pk, {})
            for dim in (f"MODEL#{model}", "TOTAL"):
                r = rows.setdefault(f"HOURLY#{hour}#{dim}", row())
                r["invocations"] += n
        return out, {}, 0
    return run


def test_settle_writes_daily_monthly_never_today_and_skips_expired_hours(ddb, monkeypatch):
    now = datetime(2026, 10, 30, 3, tzinfo=UTC)
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([
        ("pk", "2026-09-30T23", "m", 2), ("pk", "2026-10-29T10", "m", 3), ("pk", "2026-10-30T01", "m", 9),
        ("pk", "2026-06-01T00", "m", 1),
    ]))
    cc.put_meta("SETTLED#pk", {"day": "2026-09-29"})
    days = {"pk": {date(2026, 9, 30), date(2026, 10, 29), date(2026, 10, 30), date(2026, 6, 1)}}
    result = cc.settle_days(days, now, Ctx(), dry_run=False, markers={})
    assert result["next"] is None
    k = keys(ddb, "pk")
    assert "DAILY#2026-09-30#TOTAL" in k and "MONTHLY#2026-09#TOTAL" in k and "DAILY#2026-10-29#TOTAL" in k
    assert not any(s.startswith("DAILY#2026-10-30") for s in k)  # today is never a DAILY row
    assert not any(s.startswith("MONTHLY#2026-10") for s in k)   # current month is never a MONTHLY row
    assert "DAILY#2026-06-01#TOTAL" in k and not any(s.startswith("HOURLY#2026-06-01") for s in k)  # past HOURLY TTL
    assert cc.settled_day("pk", now) == date(2026, 9, 30)  # contiguous only; 10-29 waits for 10-01..10-28


def test_settle_stops_before_budget_and_reports_resume_point(ddb, monkeypatch):
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([]))
    days = {"pk": {date(2026, 8, 1), date(2026, 9, 1)}}
    result = cc.settle_days(days, datetime(2026, 10, 30, tzinfo=UTC), Ctx(seconds=10), dry_run=False, markers={})
    assert result["next"] == "2026-08-01" and result["days"] == 0


def test_incremental_recomputes_touched_and_current_hours_and_marks_completed_days(ddb, monkeypatch):
    now = datetime(2026, 10, 30, 0, 10, tzinfo=UTC)
    monkeypatch.setattr(cc, "athena_rows", lambda sql, **kw: [{"pk": "pk", "hour": "2026-10-28T05"}])
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([("pk", "2026-10-28T05", "m", 1), ("pk", "2026-10-29T23", "m", 4)]))
    cc.put_meta("L2#latest", {"last_window_end": (now - timedelta(minutes=5)).isoformat()})
    result = cc.run_incremental(now, Ctx())
    k = keys(ddb, "pk")
    assert "HOURLY#2026-10-28T05#TOTAL" in k and "HOURLY#2026-10-29T23#TOTAL" in k  # touched + previous hour
    assert cc.get_meta("DIRTY_DAY#pk#2026-10-28") and cc.get_meta("DIRTY_DAY#pk#2026-10-29")
    assert result["window_end"] == (now - cc.INGEST_SETTLE).isoformat()


# ── read path tiers (webui/api/store.py) ──

def test_read_path_uses_hourly_after_settled_watermark(monkeypatch):
    import store

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 30, 12, tzinfo=UTC)
    monkeypatch.setattr(store, "datetime", Frozen)
    seg = store._plan_segments(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 30, 12, tzinfo=UTC),
                               daily_end=datetime(2026, 10, 20, tzinfo=UTC))
    assert ("MONTHLY", "2026-09", "2026-09") in seg
    assert ("DAILY", "2026-10-01", "2026-10-19") in seg
    assert seg[-1][0] == "HOURLY" and seg[-1][1] == "2026-10-20T00"


def test_completed_month_markers_survive_until_monthly_is_written(ddb, monkeypatch):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([("pk", "2026-09-15T10", "m", 1)]))
    cc.put_meta("SETTLED#pk", {"day": "2026-10-28"})
    cc.put_meta("DIRTY_DAY#pk#2026-09-15", {"marked_at": "t1"})
    real = cc.replace_scope

    def failing(pk, prefix, *a, **k):
        if prefix.startswith("MONTHLY#"):
            raise RuntimeError("crash before MONTHLY")
        return real(pk, prefix, *a, **k)
    monkeypatch.setattr(cc, "replace_scope", failing)
    with pytest.raises(RuntimeError):
        cc.settle_days({"pk": {date(2026, 9, 15)}}, now, Ctx(), dry_run=False, markers={("pk", "2026-09-15"): "t1"})
    assert cc.get_meta("DIRTY_DAY#pk#2026-09-15")  # still there, so the next settle redoes the month
    monkeypatch.setattr(cc, "replace_scope", real)
    cc.settle_days({"pk": {date(2026, 9, 15)}}, now, Ctx(), dry_run=False, markers={("pk", "2026-09-15"): "t1"})
    assert cc.get_meta("DIRTY_DAY#pk#2026-09-15") is None and "MONTHLY#2026-09#TOTAL" in keys(ddb, "pk")


def test_count_check_mismatch_becomes_marker_and_check_reruns_after_incomplete_settle(ddb, monkeypatch):
    now = datetime(2026, 10, 30, 1, tzinfo=UTC)
    monkeypatch.setattr(cc, "iceberg_pks", lambda: {"pk"})
    monkeypatch.setattr(cc, "count_mismatches", lambda now: [("pk", date(2026, 8, 1))])
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([]))
    cc.put_meta("SETTLED#pk", {"day": "2026-10-28"})
    cc.run_settle(now, Ctx(seconds=10))  # budget exhausted immediately
    assert cc.get_meta("DIRTY_DAY#pk#2026-08-01") and cc.get_meta("COUNT_CHECK#latest") is None
    cc.run_settle(now, Ctx())
    assert cc.get_meta("COUNT_CHECK#latest")["day"] == "2026-10-30"


def test_handler_lock_busy_skips_incremental_and_settle_but_fails_rebuild(ddb, monkeypatch):
    monkeypatch.setattr(cc, "LOCK_WAIT_S", 0)
    holder = cc.Lease(Ctx(), wait_s=0).__enter__()
    assert cc.handler({}, Ctx()) == {"skipped": "lock busy"}
    assert cc.handler({"settle": {}}, Ctx()) == {"skipped": "lock busy"}
    with pytest.raises(cc.LockBusy):
        cc.handler({"rebuild": {"start": "all"}}, Ctx())
    holder.__exit__(None, None, None)


def test_incremental_failure_keeps_checkpoint_and_releases_lock(ddb, monkeypatch):
    now = cc.utcnow().replace(microsecond=0)
    cc.put_meta("L2#latest", {"last_window_end": (now - timedelta(minutes=5)).isoformat()})
    cc.put_meta("ACCOUNT#pk", {"registered_at": now.isoformat()})
    monkeypatch.setattr(cc, "athena_rows", lambda sql, **kw: [])
    monkeypatch.setattr(cc, "aggregate", lambda a, b: (_ for _ in ()).throw(RuntimeError("athena down")))
    with pytest.raises(RuntimeError):
        cc.handler({}, Ctx())
    assert cc.get_meta("L2#latest")["last_window_end"] == (now - timedelta(minutes=5)).isoformat()
    assert cc.get_meta("L2#lock") is None


def test_read_path_without_settled_reads_hourly_for_recent_days(monkeypatch):
    import store

    class FakeTable:
        def get_item(self, **kw):
            return {}
    monkeypatch.setattr(store, "_usage", lambda: FakeTable())
    end = store.daily_end_for("pk")
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert end == today - store.HOURLY_RETENTION


class CountdownCtx:
    """Remaining time drops by `step` seconds on every check (simulates running out of budget mid-month)."""

    def __init__(self, start, step):
        self.left, self.step = start, step

    def get_remaining_time_in_millis(self):
        self.left -= self.step
        return self.left * 1000


def test_budget_stop_mid_completed_month_does_not_advance_settled_or_skip_monthly(ddb, monkeypatch):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([("A", "2026-09-29T01", "m", 1), ("B", "2026-09-29T01", "m", 1)]))
    for pk in ("A", "B"):
        cc.put_meta(f"SETTLED#{pk}", {"day": "2026-09-27"})
    days = {pk: {date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)} for pk in ("A", "B")}
    result = cc.settle_days(days, now, CountdownCtx(start=400, step=60), dry_run=False, markers={})
    assert result["next"] == "2026-09-01"
    assert cc.settled_day("A", now) == date(2026, 9, 27)  # not advanced past a month whose MONTHLY is missing
    result = cc.settle_days(days, now, Ctx(), dry_run=False, markers={})
    assert result["next"] is None
    assert all("MONTHLY#2026-09#TOTAL" in keys(ddb, pk) for pk in ("A", "B"))
    assert cc.settled_day("A", now) == cc.settled_day("B", now) == date(2026, 9, 30)


def test_rebuild_resumes_at_month_start_so_no_pk_is_skipped(ddb, monkeypatch):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([(pk, "2026-09-05T01", "m", 1) for pk in ("A", "B", "C")]))
    days = {pk: {date(2026, 9, 5)} for pk in ("A", "B", "C")}
    first = cc.settle_days(days, now, CountdownCtx(start=400, step=100), dry_run=False, markers={})  # stops at C
    assert first["next"] == "2026-09-01"
    cc.settle_days(days, now, Ctx(), dry_run=False, markers={})
    assert all("DAILY#2026-09-05#TOTAL" in keys(ddb, pk) for pk in ("A", "B", "C"))


def test_unpriced_span_across_months_is_cleared_month_by_month(ddb):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    cc.put_meta("UNPRICED#m", {"first_seen": "2026-08-20T00:00:00+00:00", "last_seen": "2026-09-10T00:00:00+00:00"})
    cc.update_unpriced({}, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), now)
    assert cc.get_meta("UNPRICED#m")["first_seen"].startswith("2026-09-01")
    cc.update_unpriced({}, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC), now)
    assert cc.get_meta("UNPRICED#m") is None


def test_partial_rewrite_never_clears_unpriced_marker(ddb, monkeypatch):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    monkeypatch.setattr(cc, "aggregate", fake_aggregate([("pk", "2026-09-10T01", "m", 1)]))
    cc.put_meta("SETTLED#pk", {"day": "2026-10-28"})
    cc.put_meta("UNPRICED#m", {"first_seen": "2026-09-10T01:00:00+00:00", "last_seen": "2026-09-10T01:00:00+00:00"})
    cc.settle_days({"pk": {date(2026, 9, 29), date(2026, 9, 30)}}, now, Ctx(), dry_run=False, markers={})
    assert cc.get_meta("UNPRICED#m")  # 09-10 was not rewritten, so it still shows $0
    cc.settle_days({"pk": {date(2026, 9, 1) + timedelta(days=n) for n in range(30)}}, now, Ctx(), dry_run=False, markers={})
    assert cc.get_meta("UNPRICED#m") is None


def test_read_path_range_starting_mid_day_today(monkeypatch):
    import store

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 30, 12, 40, tzinfo=UTC)
    monkeypatch.setattr(store, "datetime", Frozen)
    today = datetime(2026, 10, 30, tzinfo=UTC)
    # "Today" for a user at UTC-7 starts at 07:00 UTC
    assert store._plan_segments(today + timedelta(hours=7), Frozen.now(), daily_end=today) == [
        ("HOURLY", "2026-10-30T07", "2026-10-30T12")]
    assert store._plan_segments(today + timedelta(hours=3), today + timedelta(hours=4), daily_end=today) == [
        ("HOURLY", "2026-10-30T03", "2026-10-30T03")]
    # a mid-day start on an earlier day still hands over to DAILY at midnight
    assert store._plan_segments(datetime(2026, 10, 20, 15, tzinfo=UTC), Frozen.now(), daily_end=today) == [
        ("HOURLY", "2026-10-20T15", "2026-10-20T23"), ("DAILY", "2026-10-21", "2026-10-29"),
        ("HOURLY", "2026-10-30T00", "2026-10-30T12")]


def test_read_path_ranges_ending_before_daily_end_off_midnight(monkeypatch):
    import store

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 30, 12, 40, tzinfo=UTC)
    monkeypatch.setattr(store, "datetime", Frozen)
    de = datetime(2026, 10, 30, tzinfo=UTC)
    plan = lambda s, e: store._plan_segments(s, e, daily_end=de)  # noqa: E731
    assert plan(datetime(2026, 10, 20, 15, tzinfo=UTC), datetime(2026, 10, 21, 10, tzinfo=UTC)) == [
        ("HOURLY", "2026-10-20T15", "2026-10-21T09")]
    assert plan(datetime(2026, 9, 30, 16, tzinfo=UTC), datetime(2026, 10, 14, 16, tzinfo=UTC)) == [  # UTC+8 Oct 1..14
        ("HOURLY", "2026-09-30T16", "2026-09-30T23"), ("DAILY", "2026-10-01", "2026-10-13"),
        ("HOURLY", "2026-10-14T00", "2026-10-14T15")]
    assert plan(datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 8, tzinfo=UTC)) == [("DAILY", "2026-07-01", "2026-07-07")]
    assert plan(datetime(2026, 8, 31, 16, tzinfo=UTC), datetime(2026, 9, 30, 16, tzinfo=UTC)) == [  # UTC+8 September
        ("HOURLY", "2026-08-31T16", "2026-08-31T23"), ("DAILY", "2026-09-01", "2026-09-29"),
        ("HOURLY", "2026-09-30T00", "2026-09-30T15")]
