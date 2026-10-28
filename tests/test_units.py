from datetime import datetime, timezone

import pytest

import aggregate_stats as agg
import bedrock_logging as bl
import lf_admin
import sync_pricing as sp
from webui.auth import LoginThrottle, credentials_match, safe_redirect

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


# ── sync_pricing ──

def test_extract_prices_covers_embedding_rerank_and_image_modes():
    catalog = {
        "global.cohere.embed-v4:0": {"litellm_provider": "bedrock", "mode": "embedding", "input_cost_per_token": 1.2e-07},
        "cohere.rerank-v3-5:0": {"litellm_provider": "bedrock", "mode": "rerank", "input_cost_per_query": 0.002},
        "stability.sd3-5-large-v1:0": {"litellm_provider": "bedrock", "mode": "image_generation", "output_cost_per_image": 0.08},
        "1024-x-1024/50-steps/stability.x": {"litellm_provider": "bedrock", "mode": "image_generation", "output_cost_per_image": 0.04},
        "openai/gpt": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-06},
    }
    prices = sp.extract_bedrock_prices(catalog)
    assert set(prices) == {"global.cohere.embed-v4:0", "cohere.rerank-v3-5:0", "stability.sd3-5-large-v1:0"}
    assert prices["global.cohere.embed-v4:0"]["input_per_1k"] == "0.00012"
    assert prices["cohere.rerank-v3-5:0"]["per_request"] == "0.002"


def test_price_comparison_is_numeric():
    assert sp.same_prices({"input_per_1k": "0.0", "output_per_1k": "0.0150"}, {"input_per_1k": "0", "output_per_1k": "0.015"})


@pytest.mark.parametrize("new, suspicious", [("0.004", False), ("0.03", True), ("0", True)])
def test_suspicious_change_guard(new, suspicious):
    assert sp.is_suspicious_change({"input_per_1k": "0.003"}, {"input_per_1k": new}) is suspicious


# ── aggregate_stats ──

def test_days_to_roll_recomputes_recent_days():
    days = agg.days_to_roll(NOW, "2026-10-27")
    assert [d.strftime("%Y-%m-%d") for d in days] == ["2026-10-25", "2026-10-26", "2026-10-27"]


def test_days_to_roll_fills_gap_after_old_watermark():
    days = agg.days_to_roll(NOW, "2026-10-20")
    assert days[0].strftime("%Y-%m-%d") == "2026-10-21" and days[-1].strftime("%Y-%m-%d") == "2026-10-27"


def test_months_to_roll_always_includes_last_completed_month():
    assert agg.months_to_roll(NOW, "2026-09") == [(2026, 9)]
    assert agg.months_to_roll(NOW, "2026-07") == [(2026, 8), (2026, 9)]


def test_aggregate_sums_and_extremes():
    rows = agg.aggregate([
        {"SK": "HOURLY#2026-10-27T01#TOTAL", "invocations": 2, "cost_request_micro": 5, "max_latency_ms": 10, "min_latency_ms": 3},
        {"SK": "HOURLY#2026-10-27T02#TOTAL", "invocations": 1, "max_latency_ms": 20, "min_latency_ms": 0},
    ])
    total = rows["TOTAL"]
    assert total["invocations"] == 3 and total["cost_request_micro"] == 5
    assert total["max_latency_ms"] == 20 and total["min_latency_ms"] == 3


# ── Bedrock logging custom resource ──

TARGET = {"bucketName": "b", "keyPrefix": "p/"}


class FakeBedrock:
    def __init__(self, config=None):
        self.config, self.puts, self.deleted = config, [], False

    def get_model_invocation_logging_configuration(self):
        return {"loggingConfig": self.config} if self.config else {}

    def put_model_invocation_logging_configuration(self, loggingConfig):
        self.puts.append(loggingConfig)
        self.config = loggingConfig

    def delete_model_invocation_logging_configuration(self):
        self.deleted, self.config = True, None


def cr_event(request_type, physical_id=None):
    ev = {"RequestType": request_type, "ResourceProperties": {"BucketName": "b", "KeyPrefix": "p/"}}
    if physical_id:
        ev["PhysicalResourceId"] = physical_id
    return ev


def test_logging_create_when_absent_then_delete():
    client = FakeBedrock()
    pid = bl.handle(cr_event("Create"), client)
    assert pid == "bedrock-invocation-logging/created" and client.config["s3Config"] == TARGET
    bl.handle(cr_event("Delete", pid), client)
    assert client.deleted


def test_logging_adopts_matching_config_and_never_deletes_it():
    client = FakeBedrock({"s3Config": TARGET, "textDataDeliveryEnabled": True})
    pid = bl.handle(cr_event("Create"), client)
    assert pid.endswith("/adopted") and not client.puts
    bl.handle(cr_event("Delete", pid), client)
    assert not client.deleted


def test_logging_refuses_to_overwrite_other_destination():
    client = FakeBedrock({"s3Config": {"bucketName": "someone-else", "keyPrefix": ""}})
    with pytest.raises(ValueError):
        bl.handle(cr_event("Create"), client)


def test_logging_merge_keeps_cloudwatch_and_removes_only_s3():
    cw = {"logGroupName": "/aws/bedrock", "roleArn": "arn:role"}
    client = FakeBedrock({"cloudWatchConfig": cw, "textDataDeliveryEnabled": True})
    pid = bl.handle(cr_event("Create"), client)
    assert pid.endswith("/merged") and client.config["cloudWatchConfig"] == cw
    bl.handle(cr_event("Delete", pid), client)
    assert not client.deleted and "s3Config" not in client.config


def test_logging_legacy_physical_id_is_adopted_and_its_delete_is_a_noop():
    client = FakeBedrock({"s3Config": TARGET})
    pid = bl.handle(cr_event("Update", "2026/05/08/[$LATEST]abc"), client)
    assert pid.endswith("/adopted")
    bl.handle(cr_event("Delete", "2026/05/08/[$LATEST]abc"), client)
    assert not client.deleted


# ── LF admin custom resource ──

class FakeLF:
    def __init__(self, admins):
        self.settings = {"DataLakeAdmins": [{"DataLakePrincipalIdentifier": a} for a in admins], "Parameters": {"X": "1"}}

    def get_data_lake_settings(self):
        return {"DataLakeSettings": {**self.settings, "DataLakeAdmins": list(self.settings["DataLakeAdmins"])}}

    def put_data_lake_settings(self, DataLakeSettings):
        self.settings = DataLakeSettings

    def admins(self):
        return [a["DataLakePrincipalIdentifier"] for a in self.settings["DataLakeAdmins"]]


def lf_event(request_type, physical_id=None):
    ev = {"RequestType": request_type, "ResourceProperties": {"Principal": "arn:me"}}
    if physical_id:
        ev["PhysicalResourceId"] = physical_id
    return ev


def test_lf_admin_merges_and_removes_only_what_it_added():
    client = FakeLF(["arn:other"])
    pid = lf_admin.handle(lf_event("Create"), client)
    assert client.admins() == ["arn:other", "arn:me"] and client.settings["Parameters"] == {"X": "1"}
    lf_admin.handle(lf_event("Delete", pid), client)
    assert client.admins() == ["arn:other"]


def test_lf_admin_leaves_preexisting_admin_on_delete():
    client = FakeLF(["arn:me"])
    pid = lf_admin.handle(lf_event("Create"), client)
    lf_admin.handle(lf_event("Delete", pid), client)
    assert client.admins() == ["arn:me"]


# ── WebUI auth helpers ──

@pytest.mark.parametrize("target, expected", [
    ("/pricing", "/pricing"), ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil.com", "/"), ("", "/"),
])
def test_safe_redirect(target, expected):
    assert safe_redirect(target) == expected


def test_credentials_match():
    assert credentials_match("u", "p", "u", "p")
    assert not credentials_match("u", "x", "u", "p")
    assert not credentials_match(None, None, "u", "p")


def test_login_throttle_locks_then_expires():
    t = LoginThrottle(max_failures=2, window_s=60)
    t.record_failure("ip", now=0)
    t.record_failure("ip", now=1)
    assert t.is_locked("ip", now=2)
    assert not t.is_locked("ip", now=100)
