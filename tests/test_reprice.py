import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import compute_cost as cc
import reprice

UTC = timezone.utc
TABLE = "usage-stats-test"
JOB_ID = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 10, 30, 3, 40, tzinfo=UTC)  # 20 minutes clear of the :20 settle


class Ctx:
    def __init__(self, seconds=600):
        self.seconds = seconds

    def get_remaining_time_in_millis(self):
        return self.seconds * 1000


def row(**kw):
    return {**{f: 0 for f in cc.SUM_FIELDS}, **kw}


def fake_aggregate(events, unpriced=None):
    def run(ts_from, ts_to):
        out = {}
        for pk, hour, n in events:
            if ts_from <= cc.parse_hour(hour) < ts_to:
                for dim in ("MODEL#m", "TOTAL"):
                    r = out.setdefault(pk, {}).setdefault(f"HOURLY#{hour}#{dim}", row())
                    r["invocations"] += n
                    r["cost_micro_usd"] += n * 10
        return out, (unpriced or {}), 0
    return run


@pytest.fixture
def ddb(monkeypatch):
    with mock_aws():
        boto3.client("dynamodb", region_name="us-west-2").create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}])
        monkeypatch.setattr(cc, "USAGE_STATS_TABLE", TABLE)
        monkeypatch.setattr(reprice, "_table", None)
        monkeypatch.setattr(cc, "utcnow", lambda: NOW)
        monkeypatch.setattr(cc, "iceberg_first_complete_days", lambda: {"A": date(2026, 8, 10), "B": date(2026, 9, 1)})
        monkeypatch.setattr(cc, "aggregate", fake_aggregate([
            ("A", "2026-08-10T01", 1), ("A", "2026-09-05T01", 2), ("B", "2026-09-05T01", 3),
            ("A", "2026-10-29T01", 4), ("A", "2026-10-30T02", 5)]))
        cc._clients.clear()
        yield boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE)
        cc._clients.clear()


def put_job(ddb, **kw):
    ddb.put_item(Item={**reprice.JOB_KEY, "job_id": JOB_ID, "status": "queued", "start": "all", "dry_run": False,
                       "started_by": "admin@example.com", "created_at": "2026-10-30T03:00:00+00:00", **kw})


def job(ddb):
    return ddb.get_item(Key=reprice.JOB_KEY)["Item"]


def get(ddb, pk, sk):
    return ddb.get_item(Key={"PK": pk, "SK": sk}).get("Item")


def test_job_runs_all_months_then_today_and_finishes(ddb):
    put_job(ddb)
    result = reprice.run_chunk(Ctx())
    j = job(ddb)
    assert result["status"] == "done" and j["status"] == "done" and j["today_done"] is True
    assert int(j["months_done"]) == 3 and int(j["months_total"]) == 3  # Aug, Sep, Oct 1..29
    assert get(ddb, "A", "MONTHLY#2026-08#TOTAL") is None  # Iceberg covers August only from 08-10
    assert int(get(ddb, "B", "MONTHLY#2026-09#TOTAL")["invocations"]) == 3
    assert int(get(ddb, "A", "DAILY#2026-10-29#TOTAL")["invocations"]) == 4
    assert get(ddb, "A", "MONTHLY#2026-10#TOTAL") is None  # current month has no MONTHLY
    assert int(get(ddb, "A", "HOURLY#2026-10-30T02#TOTAL")["invocations"]) == 5  # today step
    assert int(j["totals"]["days_changed"]) > 0 and all("cost_after" in d for d in j["diffs"])
    assert "inflight_next" not in j


def test_partly_covered_first_month_keeps_its_monthly_row(ddb):
    ddb.put_item(Item={"PK": "A", "SK": "MONTHLY#2026-08#TOTAL", "invocations": 99})
    put_job(ddb)
    reprice.run_chunk(Ctx())
    assert int(get(ddb, "A", "MONTHLY#2026-08#TOTAL")["invocations"]) == 99
    assert get(ddb, "A", "DAILY#2026-08-09#TOTAL") is None  # before first_full: never written


def test_start_is_floored_to_month_start_and_clamped_to_iceberg(ddb):
    put_job(ddb, start="2026-09-17")
    reprice.start_job(job(ddb), NOW)
    assert job(ddb)["next"] == "2026-09-01" and int(job(ddb)["months_total"]) == 2
    put_job(ddb, start="2020-01-05")
    reprice.start_job(job(ddb), NOW)
    assert job(ddb)["next"] == "2026-08-01"


def test_no_chunk_near_settle(ddb, monkeypatch):
    put_job(ddb)
    for minute, skipped in ((11, False), (12, True), (19, True), (24, True), (25, False)):
        monkeypatch.setattr(cc, "utcnow", lambda m=minute: NOW.replace(minute=m))
        assert reprice.near_settle(cc.utcnow()) is skipped
    monkeypatch.setattr(cc, "utcnow", lambda: NOW.replace(minute=15))
    assert reprice.run_chunk(Ctx()) == {"job": JOB_ID, "skipped": "settle window"}
    assert job(ddb)["status"] == "queued"


def test_budget_stops_at_month_boundary_without_counting_a_stall(ddb):
    put_job(ddb)
    reprice.run_chunk(Ctx(seconds=200))  # below MONTH_MARGIN_S: starts the job, no month
    j = job(ddb)
    assert j["status"] == "running" and j["next"] == "2026-08-01" and int(j["stalls"]) == 0 and "inflight_next" not in j


def test_month_that_does_not_fit_counts_a_stall_and_fails_after_three(ddb, monkeypatch):
    put_job(ddb)
    monkeypatch.setattr(cc, "settle_days", lambda *a, **k: {"next": "2026-08-01", "diffs": []})
    for _ in range(3):
        reprice.run_chunk(Ctx())
    j = job(ddb)
    assert j["status"] == "failed" and int(j["stalls"]) == 3 and "did not finish" in j["error"]


def test_killed_chunk_is_detected_from_inflight_marker(ddb):
    put_job(ddb, status="running", next="2026-09-01", first_full={"A": "2026-08-10"}, inflight_next="2026-09-01",
            stalls=2, months_done=1)
    assert reprice.run_chunk(Ctx())["status"] == "failed"
    assert "stopped before it finished" in job(ddb)["error"]


def test_exception_is_recorded_counted_once_and_raised(ddb, monkeypatch):
    put_job(ddb)

    def boom(*a, **k):
        raise RuntimeError("athena down")
    monkeypatch.setattr(cc, "settle_days", boom)
    with pytest.raises(RuntimeError):
        reprice.run_chunk(Ctx())
    j = job(ddb)
    assert j["status"] == "running" and int(j["stalls"]) == 1 and "athena down" in j["error"] and "inflight_next" not in j


def test_cancel_during_a_month_commits_it_then_stops(ddb, monkeypatch):
    put_job(ddb)
    real = cc.settle_days

    def cancel_while_running(*a, **k):
        ddb.update_item(Key=reprice.JOB_KEY, UpdateExpression="SET #s = :c", ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":c": "cancelling"})
        return real(*a, **k)
    monkeypatch.setattr(cc, "settle_days", cancel_while_running)
    assert reprice.run_chunk(Ctx())["status"] == "cancelled"
    j = job(ddb)
    assert j["status"] == "cancelled" and int(j["months_done"]) == 1 and j["next"] == "2026-09-01"
    assert get(ddb, "B", "DAILY#2026-09-05#TOTAL") is None  # September never started


def test_dry_run_writes_no_rows_and_skips_today(ddb):
    put_job(ddb, dry_run=True)
    reprice.run_chunk(Ctx())
    j = job(ddb)
    assert j["status"] == "done" and j["today_done"] is False and int(j["totals"]["days_changed"]) > 0
    rows = ddb.scan()["Items"]
    assert {r["PK"] for r in rows} == {"REPRICE"}


def test_replaced_job_is_never_overwritten(ddb, monkeypatch):
    put_job(ddb)
    real = cc.settle_days

    def replace(*a, **k):
        ddb.put_item(Item={**reprice.JOB_KEY, "job_id": "00000000-0000-4000-8000-000000000002", "status": "queued",
                           "start": "all", "dry_run": True})
        return real(*a, **k)
    monkeypatch.setattr(cc, "settle_days", replace)
    assert reprice.run_chunk(Ctx())["stale"] is True
    assert job(ddb)["job_id"].endswith("2") and job(ddb)["status"] == "queued"


@pytest.mark.parametrize("bad", [{"start": "2026-13-01"}, {"dry_run": "yes"}, {"job_id": "x"},
                                 {"status": "running", "next": "2026-09-05", "first_full": {}}])
def test_untrusted_record_fails_the_job(ddb, bad):
    put_job(ddb, **bad)
    assert reprice.run_chunk(Ctx())["status"] == "failed"


def test_today_step_clears_unpriced_marker(ddb, monkeypatch):
    cc.put_meta("UNPRICED#m", {"first_seen": "2026-10-30T01:10:00+00:00", "last_seen": "2026-10-30T02:00:00+00:00"})
    put_job(ddb)
    reprice.run_chunk(Ctx())
    assert cc.get_meta("UNPRICED#m") is None


def test_diffs_are_capped_with_drops_first():
    diffs = [{"period": "2026-10-01", "drop": i % 2 == 0, "cost_before": 0, "cost_after": i} for i in range(1000)]
    out = cc.trim_diffs(diffs, 300)
    assert len(out) == 300 and all(d["drop"] for d in out)
    out = cc.trim_diffs([d for d in diffs if not d["drop"]][:10], 3)
    assert [d["cost_after"] for d in out] == [19, 17, 15]


def test_incremental_commits_before_a_failing_chunk(ddb, monkeypatch):
    cc.put_meta("L2#latest", {"last_window_end": (NOW - timedelta(minutes=5)).isoformat()})
    monkeypatch.setattr(cc, "athena_rows", lambda sql, **kw: [])
    monkeypatch.setattr(reprice, "run_chunk", lambda ctx: (_ for _ in ()).throw(RuntimeError("chunk")))
    with pytest.raises(RuntimeError):
        cc.handler({}, Ctx())
    assert cc.get_meta("L2#latest")["last_window_end"] > (NOW - timedelta(minutes=5)).isoformat()
    assert cc.get_meta("L2#lock") is None


def test_tick_handler_ignores_payload(monkeypatch):
    seen = []
    monkeypatch.setattr(cc, "handler", lambda event, context: seen.append(event))
    cc.tick_handler({"rebuild": {"start": "all"}}, None)
    assert seen == [{}]


def test_webui_attribute_list_matches_the_iam_condition(monkeypatch):
    import store
    monkeypatch.syspath_prepend(str(Path(__file__).parent.parent / "deploy"))
    spec = importlib.util.spec_from_file_location("webui_construct", Path(__file__).parent.parent / "deploy" / "webui.py")
    construct = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(construct)
    assert tuple(construct.REPRICE_WEBUI_ATTRIBUTES) == tuple(store.REPRICE_WEBUI_ATTRIBUTES)


def test_failure_while_starting_is_recorded_and_fails_after_three(ddb, monkeypatch):
    put_job(ddb)
    monkeypatch.setattr(cc, "iceberg_first_complete_days", lambda: (_ for _ in ()).throw(RuntimeError("athena timeout")))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            reprice.run_chunk(Ctx())
    j = job(ddb)
    assert j["status"] == "failed" and "athena timeout" in j["error"]


def test_dry_run_leaves_meta_untouched(ddb):
    cc.put_meta("SETTLED#A", {"day": "2026-08-31"})
    cc.put_meta("DIRTY_DAY#A#2026-09-05", {"marked_at": "t"})
    cc.put_meta("UNPRICED#legacy", {"last_seen": "2026-07-01T00:00:00+00:00"})
    before = sorted((i["SK"], str(i)) for i in ddb.scan()["Items"] if i["PK"] == "META")
    put_job(ddb, dry_run=True)
    reprice.run_chunk(Ctx())
    assert sorted((i["SK"], str(i)) for i in ddb.scan()["Items"] if i["PK"] == "META") == before


def test_full_reprice_clears_legacy_markers_after_it_has_started(ddb):
    cc.put_meta("UNPRICED#legacy", {"last_seen": "2026-07-01T00:00:00+00:00"})
    put_job(ddb)
    reprice.start_job(job(ddb), NOW)
    assert cc.get_meta("UNPRICED#legacy") is None and job(ddb)["status"] == "running"


def test_incremental_only_skips_the_chunk(ddb, monkeypatch):
    cc.put_meta("L2#latest", {"last_window_end": (NOW - timedelta(minutes=5)).isoformat()})
    monkeypatch.setattr(cc, "athena_rows", lambda sql, **kw: [])
    put_job(ddb)
    assert "reprice" not in cc.handler({"incremental_only": True}, Ctx())
    assert job(ddb)["status"] == "queued"


def test_cancel_before_today_step_cancels(ddb, monkeypatch):
    put_job(ddb, status="running", next="2026-11-01", first_full={"A": "2026-08-10"}, months_done=3, stalls=0)
    real = reprice.load_job

    def cancelling():
        ddb.update_item(Key=reprice.JOB_KEY, UpdateExpression="SET #s = :c", ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":c": "cancelling"})
        return real()
    monkeypatch.setattr(reprice, "load_job", cancelling)
    with pytest.raises(reprice.Stale):  # run_chunk turns this into "cancelled"
        reprice.advance(job(ddb), reprice.Deadline(Ctx(), 420))
    assert get(ddb, "A", "HOURLY#2026-10-30T02#TOTAL") is None
