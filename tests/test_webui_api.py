import base64
import gzip
import json
import time
from urllib.parse import quote

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import app as webui
import auth
import store

REGION, POOL, CLIENT = "us-west-2", "us-west-2_TEST", "client123"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def token(key=KEY, **overrides):
    claims = {"iss": ISSUER, "exp": int(time.time()) + 600, "token_use": "access", "client_id": CLIENT,
              "username": "alice", "cognito:groups": [], **overrides}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


VERIFIER = auth.TokenVerifier(REGION, POOL, CLIENT, jwks_client=FakeJwks())
ADMIN = auth.ADMIN_GROUP


# ── auth ──

def test_valid_viewer_token():
    user = VERIFIER.verify(f"Bearer {token()}")
    assert user.username == "alice" and not user.is_admin


@pytest.mark.parametrize("bad", [
    {"key": OTHER_KEY},                       # wrong signature
    {"exp": int(time.time()) - 120},          # expired beyond leeway
    {"token_use": "id"},                      # ID token instead of access token
    {"client_id": "another-app"},             # token for another client
    {"iss": "https://cognito-idp.us-west-2.amazonaws.com/other"},
    {"client_id": None},                      # missing required claim
])
def test_rejected_tokens(bad):
    key = bad.pop("key", KEY)
    with pytest.raises(auth.AuthError) as e:
        VERIFIER.verify(f"Bearer {token(key=key, **bad)}")
    assert e.value.status == 401


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer"])
def test_missing_bearer(header):
    with pytest.raises(auth.AuthError):
        VERIFIER.verify(header)


def test_any_signed_in_user_can_view_only_admin_group_can_edit():
    admin = auth.User("a", frozenset({ADMIN, "other"}))
    viewer = auth.User("v", frozenset({"some-other-group"}))
    assert auth.require(admin, admin=True) is admin
    assert auth.require(viewer) is viewer and auth.require(auth.User("n", frozenset())).username == "n"
    with pytest.raises(auth.AuthError) as e:
        auth.require(viewer, admin=True)
    assert e.value.status == 403


# ── routing ──

@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(webui, "_verifier", VERIFIER)
    monkeypatch.setattr(webui, "user_email", lambda username: f"{username}@example.com")
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>" + "x" * 2000 + "</html>")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1);" * 200)
    monkeypatch.setattr(webui, "STATIC_DIR", tmp_path.resolve())
    webui._load.cache_clear()
    return monkeypatch


def call(path, method="GET", query=None, groups=(), body=None, auth_header=True, headers=None):
    hdrs = {"host": "alb.internal", **(headers or {})}
    if auth_header:
        hdrs["authorization"] = f"Bearer {token(**{'cognito:groups': list(groups)})}"
    # ALB forwards query strings still percent-encoded
    encoded = {quote(k, safe=""): quote(v, safe="") for k, v in (query or {}).items()}
    event = {"requestContext": {"elb": {"targetGroupArn": "arn:tg"}}, "httpMethod": method, "path": path,
             "queryStringParameters": encoded, "headers": hdrs,
             "body": json.dumps(body) if body is not None else "", "isBase64Encoded": False}
    resp = webui.handler(event, None)
    raw = resp.get("body") or ""
    data = base64.b64decode(raw) if resp.get("isBase64Encoded") else raw.encode()
    return resp["statusCode"], resp.get("headers", {}), data


RANGE = {"start": "2026-10-21T00:00:00+08:00", "end": "2026-10-28T00:00:00+08:00"}


def test_dashboard_passes_validated_inputs(api):
    seen = {}
    api.setattr(store, "get_dashboard_bundle", lambda acct, s, e, tz: seen.update(acct=acct, tz=str(tz)) or {"summary": {"cost_usd": 1.5}})
    status, headers, body = call("/api/dashboard", query={**RANGE, "account": "111122223333#us-west-2", "tz": "Asia/Singapore"})
    assert status == 200 and json.loads(body)["summary"]["cost_usd"] == 1.5
    assert seen == {"acct": "111122223333#us-west-2", "tz": "Asia/Singapore"}
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("query, message", [
    ({**RANGE, "account": "bad"}, "account"),
    ({"start": RANGE["start"], "account": "111122223333#us-west-2"}, "end"),
    ({"start": "2026-10-28T00:00:00", "end": RANGE["end"], "account": "111122223333#us-west-2"}, "UTC offset"),
    ({"start": RANGE["end"], "end": RANGE["start"], "account": "111122223333#us-west-2"}, "after start"),
    ({"start": "2020-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z", "account": "111122223333#us-west-2"}, "exceed"),
    ({**RANGE, "account": "111122223333#us-west-2", "tz": "Mars/Base"}, "IANA"),
])
def test_dashboard_rejects_bad_input(api, query, message):
    status, _, body = call("/api/dashboard", query=query)
    assert status == 400 and message in json.loads(body)["error"]


def test_trend_dimension_validation(api):
    api.setattr(store, "get_trend", lambda *a: [])
    ok = call("/api/trend", query={**RANGE, "account": "111122223333#us-west-2", "dim": "MODEL#global.anthropic.claude-opus-5-5"})
    bad = call("/api/trend", query={**RANGE, "account": "111122223333#us-west-2", "dim": "DEDUP#x"})
    assert ok[0] == 200 and bad[0] == 400


def test_unauthenticated_rejected_and_groupless_user_can_read(api):
    api.setattr(store, "get_accounts", lambda: [])
    assert call("/api/accounts", auth_header=False)[0] == 401
    assert call("/api/accounts", groups=())[0] == 200


def test_pricing_write_requires_admin(api):
    saved = {}
    api.setattr(store, "save_pricing", lambda m, p, d, updated_by: saved.update(m=m, p=p, d=d, by=updated_by))
    body = {"effective_date": "2026-11-01T08:00:00+08:00", "input_per_1k": 0.003, "output_per_1k": 0.015}
    assert call("/api/pricing/cohere.rerank-v3-5%3A0", method="PUT", body=body)[0] == 403
    status, _, _ = call("/api/pricing/cohere.rerank-v3-5%3A0", method="PUT", body=body, groups=[ADMIN])
    assert status == 204
    assert saved["m"] == "cohere.rerank-v3-5:0" and saved["d"] == "2026-11-01T00:00:00Z" and saved["by"] == "alice@example.com"
    assert saved["p"]["per_request"] == 0.0


@pytest.mark.parametrize("bad", [{"input_per_1k": -1}, {"input_per_1k": "1"}, {"input_per_1k": True}, {"per_request": 5000}])
def test_pricing_write_validates_prices(api, bad):
    api.setattr(store, "save_pricing", lambda *a, **k: pytest.fail("must not save"))
    body = {"effective_date": "2026-11-01T00:00:00Z", **bad}
    assert call("/api/pricing/m", method="PUT", body=body, groups=[ADMIN])[0] == 400


def test_unknown_api_route_is_json_404(api):
    status, headers, body = call("/api/nope")
    assert status == 404 and json.loads(body)["error"] == "Not found"


# ── static ──

def test_config_json_is_public(api):
    status, _, body = call("/config.json", auth_header=False)
    assert status == 200 and set(json.loads(body)) == {"region", "userPoolId", "userPoolClientId"}


def test_spa_fallback_serves_index_with_csp(api):
    status, headers, body = call("/pricing", auth_header=False, headers={"accept-encoding": "gzip, br"})
    assert status == 200 and headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(body).startswith(b"<html>")
    assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-cache"


def test_hashed_asset_is_immutable_and_uncompressed_without_accept_encoding(api):
    status, headers, body = call("/assets/app-abc123.js", auth_header=False)
    assert status == 200 and body.startswith(b"console.log")
    assert "immutable" in headers["Cache-Control"] and "Content-Encoding" not in headers


def test_path_traversal_falls_back_to_index(api):
    status, _, body = call("/../../etc/passwd", auth_header=False)
    assert status == 200 and body.startswith(b"<html>")


# ── store: segment planning and rebucketing ──

def test_plan_segments_uses_coarsest_complete_tiers():
    from datetime import datetime, timezone
    segs = store._plan_segments(datetime(2026, 8, 15, 5, tzinfo=timezone.utc), datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert segs == [("HOURLY", "2026-08-15T05", "2026-08-15T23"), ("DAILY", "2026-08-16", "2026-08-31"),
                    ("MONTHLY", "2026-09", "2026-09"), ("DAILY", "2026-10-01", "2026-10-02")]


def test_rebucket_groups_hours_into_local_days():
    from zoneinfo import ZoneInfo
    rows = [{"period": "2026-10-27T15", "granularity": "HOURLY", "invocations": 1, "cost_micro_usd": 1_000_000},
            {"period": "2026-10-27T17", "granularity": "HOURLY", "invocations": 2, "cost_micro_usd": 500_000}]
    out = store._rebucket(rows, "DAILY", ZoneInfo("Asia/Singapore"))  # 15:00Z = 23:00 local, 17:00Z = next day
    assert [(r["period"], r["invocations"], r["cost_usd"]) for r in out] == [("2026-10-27", 1, 1.0), ("2026-10-28", 2, 0.5)]


def test_oversized_asset_is_gzipped_even_without_accept_encoding(api, tmp_path):
    (tmp_path / "assets" / "big-abc.css").write_text(".a{color:red}" * 60_000)  # ~780 KB raw
    status, headers, body = call("/assets/big-abc.css", auth_header=False)
    assert status == 200 and headers["Content-Encoding"] == "gzip" and len(body) < 100_000


def test_encoded_offset_and_caller_dimension_are_decoded(api):
    seen = {}
    api.setattr(store, "get_trend", lambda acct, s, e, dim, tz: seen.update(start=s.isoformat(), dim=dim) or [])
    status, _, _ = call("/api/trend", query={"start": "2026-10-21T00:00:00+08:00", "end": "2026-10-28T00:00:00+08:00",
                                             "account": "111122223333#us-west-2", "dim": "CALLER#role/My Session"})
    assert status == 200 and seen == {"start": "2026-10-21T00:00:00+08:00", "dim": "CALLER#role/My Session"}


# ── reprice ──

@pytest.fixture
def reprice_api(api):
    from moto import mock_aws
    import boto3
    with mock_aws():
        boto3.client("dynamodb", region_name="us-west-2").create_table(
            TableName="usage-test", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}])
        api.setattr(store, "USAGE_TABLE", "usage-test")
        api.setattr(store, "_clients", {})
        kicks = []
        api.setattr(store, "kick_reprice", lambda: kicks.append(1))
        yield kicks


def test_reprice_start_validates_and_allows_one_active_job(reprice_api):
    assert call("/api/reprice", "POST", body={"start": "all"})[0] == 403  # viewers can't
    for bad in ({"start": "2026-02-30"}, {"start": "yesterday"}, {"start": "all", "dry_run": "true"},
                {"start": "all", "extra": 1}, {"start": "2999-01-01"}):
        assert call("/api/reprice", "POST", groups=[ADMIN], body=bad)[0] == 400, bad
    status, _, body = call("/api/reprice", "POST", groups=[ADMIN], body={"start": "2026-09-17", "dry_run": True})
    job = json.loads(body)["job"]
    assert status == 202 and job["status"] == "queued" and job["dry_run"] is True and job["started_by"].endswith("@example.com")
    assert reprice_api == [1]
    assert set(store.get_reprice_job()) <= set(store.REPRICE_WEBUI_ATTRIBUTES)
    status, _, body = call("/api/reprice", "POST", groups=[ADMIN], body={"start": "all"})
    assert status == 409 and json.loads(body)["job"]["job_id"] == job["job_id"]


def test_reprice_view_hides_admin_fields_from_viewers(reprice_api):
    call("/api/reprice", "POST", groups=[ADMIN], body={"start": "all"})
    store._usage().update_item(Key=store.REPRICE_KEY, UpdateExpression="SET diffs = :d, first_full = :f",
                               ExpressionAttributeValues={":d": [], ":f": {}})
    viewer = json.loads(call("/api/reprice")[2])["job"]
    admin = json.loads(call("/api/reprice", groups=[ADMIN])[2])["job"]
    assert "started_by" not in viewer and "diffs" not in viewer and "first_full" not in viewer and "PK" not in viewer
    assert "started_by" in admin and "diffs" in admin and "first_full" not in admin
    assert json.loads(call("/api/status")[2])["reprice"]["status"] == "queued"


def test_reprice_cancel_transitions(reprice_api):
    job_id = json.loads(call("/api/reprice", "POST", groups=[ADMIN], body={"start": "all"})[2])["job"]["job_id"]
    assert call("/api/reprice", "DELETE", query={"job_id": job_id})[0] == 403
    assert call("/api/reprice", "DELETE", groups=[ADMIN], query={"job_id": "nope"})[0] == 400
    assert call("/api/reprice", "DELETE", groups=[ADMIN], query={"job_id": "00000000-0000-4000-8000-000000000009"})[0] == 404
    status, _, body = call("/api/reprice", "DELETE", groups=[ADMIN], query={"job_id": job_id})
    assert status == 200 and json.loads(body)["job"]["status"] == "cancelled"
    call("/api/reprice", "POST", groups=[ADMIN], body={"start": "all"})  # a finished job can be replaced
    running = store.get_reprice_job()
    store._usage().update_item(Key=store.REPRICE_KEY, UpdateExpression="SET #s = :r", ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues={":r": "running"})
    body = call("/api/reprice", "DELETE", groups=[ADMIN], query={"job_id": running["job_id"]})[2]
    assert json.loads(body)["job"]["status"] == "cancelling"


def test_reprice_start_returns_202_when_kick_fails(reprice_api):
    def boom(**kw):
        raise RuntimeError("endpoint unreachable")
    store._clients["lambda"] = type("L", (), {"invoke": staticmethod(boom)})()
    store.REPRICE_TICK_FUNCTION = "tick"
    try:
        store.kick_reprice()  # logs a warning instead of raising
    finally:
        store.REPRICE_TICK_FUNCTION = ""
    assert call("/api/reprice", "POST", groups=[ADMIN], body={"start": "all"})[0] == 202
