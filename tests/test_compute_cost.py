from datetime import datetime, timedelta, timezone

import compute_cost as cc

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
PRICE = {"input": 3000, "output": 15000, "cache_read": 300, "cache_write": 3750, "cache_write_1h": 6000, "request": 0}


def event(**overrides):
    ev = {"account_id": "111", "region": "us-west-2", "request_id": "r1", "ts": "2026-10-28 11:05:09.000 UTC",
          "model_id": "m", "caller": "role/s", "input_tokens": "1000", "output_tokens": "200",
          "cache_read_tokens": "0", "cache_write_total_tokens": "0", "latency_ms": "2000"}
    return {**ev, **overrides}


def test_plan_window_resumes_from_checkpoint_with_overlap():
    watermark = NOW - timedelta(minutes=10)
    start, end, new_wm = cc.plan_window(NOW, watermark)
    assert start == watermark - cc.OVERLAP
    assert end == NOW - cc.SETTLE
    assert new_wm == end


def test_plan_window_caps_catch_up():
    watermark = NOW - timedelta(days=1)
    _, end, new_wm = cc.plan_window(NOW, watermark)
    assert end == watermark + cc.MAX_CATCHUP == new_wm


def test_plan_window_never_moves_watermark_back():
    watermark = NOW  # e.g. clock skew between runs
    _, _, new_wm = cc.plan_window(NOW, watermark)
    assert new_wm == watermark


def test_to_metrics_costs_and_dims():
    m = cc.to_metrics(event(), lambda model, ts: PRICE)
    assert m.pk == "111#us-west-2" and m.hour_key == "2026-10-28T11"
    assert m.dims == ["MODEL#m", "TOTAL", "CALLER#role/s"]
    assert m.sums["cost_input_micro"] == 3000 and m.sums["cost_output_micro"] == 3000
    assert m.sums["cost_micro_usd"] == 6000
    assert m.extremes == {"max_latency_ms": 2000, "min_latency_ms": 2000, "tpot_max": 10000, "tpot_min": 10000}
    assert not m.unpriced


def test_to_metrics_unpriced_model_is_counted_and_flagged():
    m = cc.to_metrics(event(), lambda model, ts: None)
    assert m.unpriced and m.sums["invocations"] == 1 and m.sums["cost_micro_usd"] == 0


def test_cache_write_split_and_converse_fallback():
    split = cc.to_metrics(event(cache_write_total_tokens="300", cache_write_5m_tokens="100",
                                cache_write_1h_tokens="200"), lambda *_: PRICE)
    assert split.sums["cost_cache_write_micro"] == 375 + 1200
    converse = cc.to_metrics(event(cache_write_total_tokens="300"), lambda *_: PRICE)
    assert converse.sums["cost_cache_write_micro"] == 1125  # all priced as 5-minute writes


def test_per_request_price():
    m = cc.to_metrics(event(input_tokens="0", output_tokens="0"), lambda *_: {**PRICE, "request": 2000})
    assert m.sums["cost_request_micro"] == 2000 and m.sums["cost_micro_usd"] == 2000


def test_cost_rounds_instead_of_truncating():
    assert cc._cost(30, 120) == 4   # 3.6 micro-USD
    assert cc._cost(10, 120) == 1   # 1.2


def test_unusable_events_are_skipped():
    assert cc.to_metrics(event(request_id=""), lambda *_: PRICE) is None
    assert cc.to_metrics(event(ts="garbage"), lambda *_: PRICE) is None


def test_merge_into_keeps_extremes():
    row = {f: 0 for f in cc.SUM_FIELDS}
    for latency in ("3000", "1000"):
        cc.merge_into(row, cc.to_metrics(event(latency_ms=latency), lambda *_: PRICE))
    assert row["invocations"] == 2 and row["max_latency_ms"] == 3000 and row["min_latency_ms"] == 1000


def test_rebuild_bounds_stay_outside_incremental_reach():
    start, end = cc.rebuild_bounds({"start": "2026-10-01", "end": "2026-10-31"}, NOW, NOW - timedelta(minutes=5))
    assert start == datetime(2026, 10, 1, tzinfo=timezone.utc)
    reach = NOW - timedelta(minutes=5) - cc.OVERLAP - cc.TS_LOOKBACK
    assert end <= reach and end.minute == 0


def test_rebuild_bounds_empty_when_range_is_recent():
    start, end = cc.rebuild_bounds({"start": "2026-10-28"}, NOW, NOW)
    assert end <= start
