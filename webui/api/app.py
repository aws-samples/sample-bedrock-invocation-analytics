# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""WebUI Lambda behind an internal ALB: JSON API under /api/*, SPA static files for everything else."""

import gzip
import json
import mimetypes
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aws_lambda_powertools.event_handler import ALBResolver, Response

import store
from auth import AuthError, TokenVerifier, User, require

REGION = os.environ.get("AWS_REGION", "us-west-2")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
STATIC_DIR = Path(os.environ.get("STATIC_DIR") or Path(__file__).parent / "static").resolve()
MAX_RANGE = timedelta(days=400)
MAX_PRICE = 1000.0

ACCOUNT_RE = re.compile(r"^\d{12}#[a-z0-9-]{1,32}$")
MODEL_RE = re.compile(r"^[\w.:/@+-]{1,256}$")
DIM_RE = re.compile(r"^(TOTAL|MODEL#[\w.:/@+-]{1,256}|CALLER#[^\x00-\x1f]{1,512})$")
COMPRESSIBLE = ("text/", "application/javascript", "application/json", "image/svg+xml")
# ALB caps Lambda responses at 1 MB (base64 body included); files above this are gzipped even without Accept-Encoding.
FORCE_GZIP_BYTES = 700 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}



def _json_default(value):
    """DynamoDB returns numbers as Decimal."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


app = ALBResolver(serializer=lambda obj: json.dumps(obj, default=_json_default, separators=(",", ":")))
_verifier: TokenVerifier | None = None


class BadRequest(Exception):
    pass


def verifier() -> TokenVerifier:
    global _verifier
    _verifier = _verifier or TokenVerifier(REGION, USER_POOL_ID, USER_POOL_CLIENT_ID)
    return _verifier


def current_user(admin: bool = False) -> User:
    return require(verifier().verify(app.current_event.headers.get("authorization")), admin=admin)


# ── Input parsing ──

def query_param(name: str, required: bool = True, default: str = "") -> str:
    value = (app.current_event.query_string_parameters or {}).get(name)
    if value is None:
        if required:
            raise BadRequest(f"Missing query parameter: {name}")
        return default
    return value


def parse_account(value: str) -> str:
    if not ACCOUNT_RE.match(value):
        raise BadRequest("account must look like 123456789012#us-west-2")
    return value


def parse_time(name: str, value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise BadRequest(f"{name} must be an ISO 8601 timestamp") from e
    if dt.tzinfo is None:
        raise BadRequest(f"{name} must include a UTC offset")
    return dt


def parse_range() -> tuple[datetime, datetime]:
    start, end = parse_time("start", query_param("start")), parse_time("end", query_param("end"))
    if end <= start:
        raise BadRequest("end must be after start")
    if end - start > MAX_RANGE:
        raise BadRequest(f"Range must not exceed {MAX_RANGE.days} days")
    return start, end


def parse_tz(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise BadRequest("tz must be an IANA time zone name") from e


def parse_model(value: str) -> str:
    if not MODEL_RE.match(value):
        raise BadRequest("Invalid model id")
    return value


def parse_prices(body: dict) -> dict[str, float]:
    prices = {}
    for field in store.PRICE_FIELDS:
        raw = body.get(field, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BadRequest(f"{field} must be a number")
        if not 0 <= raw <= MAX_PRICE:
            raise BadRequest(f"{field} must be between 0 and {MAX_PRICE}")
        prices[field] = float(raw)
    return prices


# ── API routes ──

@app.get("/api/accounts")
def accounts():
    current_user()
    return {"accounts": store.get_accounts()}


@app.get("/api/dashboard")
def dashboard():
    current_user()
    start, end = parse_range()
    return store.get_dashboard_bundle(parse_account(query_param("account")), start, end,
                                      parse_tz(query_param("tz", required=False, default="UTC")))


@app.get("/api/trend")
def trend():
    current_user()
    start, end = parse_range()
    dim = query_param("dim", required=False, default="TOTAL")
    if not DIM_RE.match(dim):
        raise BadRequest("dim must be TOTAL, MODEL#<id> or CALLER#<name>")
    return {"trend": store.get_trend(parse_account(query_param("account")), start, end, dim,
                                     parse_tz(query_param("tz", required=False, default="UTC")))}


@app.get("/api/ttft")
def ttft():
    current_user()
    start, end = parse_range()
    return {"ttft": store.get_ttft_trend(parse_model(query_param("model")), start, end)}


@app.get("/api/status")
def status():
    user = current_user()
    checkpoint = store.get_l2_checkpoint() or {}
    return {
        "data_up_to": checkpoint.get("last_window_end"),
        "unpriced_models": store.unpriced_models(),
        "pricing_sync": store.get_pricing_sync_info(),
        "user": {"username": user.username, "is_admin": user.is_admin},
    }


@app.get("/api/pricing")
def pricing_list():
    current_user()
    return {"models": store.get_all_pricing()}


@app.get("/api/pricing/<model>")
def pricing_history(model: str):
    current_user()
    return {"history": store.get_pricing_history(parse_model(model))}


@app.put("/api/pricing/<model>")
def pricing_save(model: str):
    user = current_user(admin=True)
    try:
        body = app.current_event.json_body or {}
    except json.JSONDecodeError as e:
        raise BadRequest("Body must be JSON") from e
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    effective_date = parse_time("effective_date", str(body.get("effective_date", "")))
    effective = effective_date.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.save_pricing(parse_model(model), parse_prices(body), effective, updated_by=user.username)
    return Response(status_code=204, body="")


@app.delete("/api/pricing/<model>")
def pricing_delete(model: str):
    current_user(admin=True)
    effective = parse_time("effective_date", query_param("effective_date"))
    store.delete_pricing(parse_model(model), effective.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return Response(status_code=204, body="")


# ── SPA ──

@app.get("/config.json")
def runtime_config():
    return Response(status_code=200, content_type="application/json", headers={"Cache-Control": "no-store"},
                    body=json.dumps({"region": REGION, "userPoolId": USER_POOL_ID, "userPoolClientId": USER_POOL_CLIENT_ID}))


@app.not_found
def static_or_index(_exc):
    path = app.current_event.path
    if path.startswith("/api/") or app.current_event.http_method not in ("GET", "HEAD"):
        return json_error(404, "Not found")
    resolved = resolve_static(path)
    if resolved is None:  # client-side route
        resolved = STATIC_DIR / "index.html"
    accepts_gzip = "gzip" in (app.current_event.headers.get("accept-encoding") or "")
    return static_response(resolved, accepts_gzip)


def resolve_static(path: str) -> Path | None:
    """Map a URL path to a file under STATIC_DIR; None for directories, missing files or traversal attempts."""
    candidate = (STATIC_DIR / path.lstrip("/")).resolve()
    if candidate == STATIC_DIR or STATIC_DIR not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def static_response(file: Path, accepts_gzip: bool) -> Response:
    content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    compressible = content_type.startswith(COMPRESSIBLE)
    body, compressed = _load(file, compressible and (accepts_gzip or file.stat().st_size > FORCE_GZIP_BYTES))
    hashed = file.parent.name == "assets"  # Vite fingerprints everything under assets/
    headers = {"Cache-Control": "public, max-age=31536000, immutable" if hashed else "no-cache",
               "Vary": "Accept-Encoding", **SECURITY_HEADERS}
    if compressed:
        headers["Content-Encoding"] = "gzip"
    if file.name == "index.html":
        headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            f"font-src 'self' data:; connect-src 'self' https://cognito-idp.{REGION}.amazonaws.com; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
    return Response(status_code=200, content_type=content_type, body=body, headers=headers, compress=False)


@lru_cache(maxsize=256)
def _load(file: Path, gzip_it: bool) -> tuple[bytes, bool]:
    data = file.read_bytes()
    if gzip_it and len(data) > 1024:
        return gzip.compress(data, compresslevel=6, mtime=0), True
    return data, False


# ── Errors and serialization ──

def json_error(status: int, message: str) -> Response:
    return Response(status_code=status, content_type="application/json", body=json.dumps({"error": message}),
                    headers=SECURITY_HEADERS)


@app.exception_handler(AuthError)
def handle_auth_error(e: AuthError):
    return json_error(e.status, str(e))


@app.exception_handler(BadRequest)
def handle_bad_request(e: BadRequest):
    return json_error(400, str(e))


def handler(event, context):
    response = app.resolve(event, context)
    if (event.get("path") or "").startswith("/api/"):
        if "multiValueHeaders" in response:
            response["multiValueHeaders"].setdefault("Cache-Control", ["no-store"])
        else:
            response.setdefault("headers", {}).setdefault("Cache-Control", "no-store")
    return response
