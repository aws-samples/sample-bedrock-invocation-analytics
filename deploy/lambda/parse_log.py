# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda L1: Parse Bedrock invocation log files from S3 and forward events to Firehose.

Flow: S3 (.json.gz) → parse NDJSON → normalize → firehose.put_record() → Iceberg usage_events

- No DDB writes here; L2 (compute_cost) is responsible for cost + aggregation
- Firehose unique_keys=[request_id] handles upsert; duplicates on retry are safe
- Body parsing branches on body type (dict vs list), not operation, for future-proofing
- error_code events are still forwarded to Iceberg; L2 filters them when aggregating
"""

import gzip
import json
import os
import re
import time
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3")

# Spoke mode: assume hub's cross-account role to call hub's Firehose
HUB_ROLE_ARN = os.environ.get("HUB_ROLE_ARN")
FIREHOSE_STREAM = os.environ["FIREHOSE_STREAM"]
HUB_REGION = os.environ.get("HUB_REGION", os.environ.get("AWS_REGION", "us-west-2"))
SCHEMA_VERSION = 1

from datetime import timedelta

_sts = boto3.client("sts") if HUB_ROLE_ARN else None
_hub_creds_expiry = None
_firehose_client = None


def _get_firehose():
    """Return a Firehose client, reusing the cached one while STS creds are fresh.
    Creating a boto3.Session + client per invocation adds ~30s on cold paths;
    the spoke path was seeing avg duration 10x hub because of this."""
    global _firehose_client, _hub_creds_expiry
    now = datetime.now(timezone.utc)

    if not HUB_ROLE_ARN:
        # Hub mode — single static client
        if _firehose_client is None:
            _firehose_client = boto3.client("firehose", region_name=HUB_REGION)
        return _firehose_client

    # Spoke mode — refresh when STS creds near expiry
    if _firehose_client is None or _hub_creds_expiry is None or now >= _hub_creds_expiry:
        creds = _sts.assume_role(RoleArn=HUB_ROLE_ARN, RoleSessionName="spoke-parse-log")["Credentials"]
        _hub_creds_expiry = creds["Expiration"].replace(tzinfo=timezone.utc) - timedelta(minutes=10)
        _firehose_client = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=HUB_REGION,
        ).client("firehose")
    return _firehose_client


firehose = _get_firehose()


def handler(event, context):
    """EventBridge S3 event handler."""
    global firehose
    firehose = _get_firehose()  # no-op if cached client is still valid

    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name")
    key = detail.get("object", {}).get("key")

    if not bucket or not key:
        print(f"Invalid event: {json.dumps(event)}")
        return

    # Skip non-log files
    if not key.endswith(".json.gz"):
        return
    if "/data/" in key or "permission-check" in key:
        return

    # Extract accountId and region from S3 path:
    # {prefix}AWSLogs/{accountId}/BedrockModelInvocationLogs/{region}/YYYY/MM/DD/HH/*.json.gz
    m = re.search(r"AWSLogs/(\d+)/BedrockModelInvocationLogs/([\w-]+)/", key)
    if not m:
        print(f"Cannot parse account/region from key: {key}")
        return

    path_account_id, path_region = m.group(1), m.group(2)

    try:
        process_file(bucket, key, path_account_id, path_region)
    except Exception as e:
        print(f"Error processing s3://{bucket}/{key}: {e}")
        raise


def process_file(bucket, key, path_account_id, path_region):
    """Download, parse, and forward each record in a single log file."""
    resp = s3.get_object(Bucket=bucket, Key=key)
    raw = gzip.decompress(resp["Body"].read()).decode("utf-8")

    # Batch records for firehose.put_record_batch (up to 500 records / 4MB per batch)
    batch = []
    source_s3_key = f"s3://{bucket}/{key}"

    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"Skipping malformed JSON line in {key}: {e}")
            continue

        event = build_event(record, path_account_id, path_region, source_s3_key)
        if event is None:
            continue
        batch.append({"Data": (json.dumps(event) + "\n").encode("utf-8")})

        # Flush every 500 records to stay under Firehose batch limit
        if len(batch) >= 500:
            put_batch(batch)
            batch = []

    if batch:
        put_batch(batch)


def put_batch(records):
    """Send a batch of records to Firehose."""
    resp = firehose.put_record_batch(DeliveryStreamName=FIREHOSE_STREAM, Records=records)
    failed = resp.get("FailedPutCount", 0)
    if failed:
        # Firehose will retry async; log for visibility but don't fail the Lambda
        print(f"Firehose put_record_batch: {failed}/{len(records)} failed (will retry)")


def build_event(record, path_account_id, path_region, source_s3_key):
    """Transform a raw Bedrock log record into the Iceberg usage_events schema.

    Returns a dict matching the column layout, or None to skip this record.
    """
    timestamp_str = record.get("timestamp", "")
    if not parse_ts(timestamp_str):
        print(f"Cannot parse timestamp: {timestamp_str}")
        return None

    model_id = record.get("modelId") or "unknown"
    if model_id.startswith("arn:"):
        model_id = model_id.rsplit("/", 1)[-1]

    account_id = record.get("accountId") or path_account_id
    region = record.get("region") or path_region

    request_id = record.get("requestId") or ""
    if not request_id:
        # No request_id → skip (Firehose upsert needs a key)
        print(f"Record has no requestId, skipping")
        return None

    inp = record.get("input") or {}
    out = record.get("output") or {}
    input_tokens = _as_int(inp.get("inputTokenCount"))
    output_tokens = _as_int(out.get("outputTokenCount"))
    cache_read_tokens = _as_int(inp.get("cacheReadInputTokenCount"))
    cache_write_total = _as_int(inp.get("cacheWriteInputTokenCount"))

    # Extract usage from body (dict or list — branch on structure, not operation)
    body = out.get("outputBodyJson")
    usage = {}
    metrics = {}
    if isinstance(body, dict):
        usage = body.get("usage") or {}
        metrics = body.get("metrics") or {}
    elif isinstance(body, list):
        # Streaming — walk chunks until we find usage
        for chunk in body:
            if not isinstance(chunk, dict):
                continue
            # Anthropic message_start shape (InvokeModelWithResponseStream)
            if chunk.get("type") == "message_start":
                msg = chunk.get("message") or {}
                usage = msg.get("usage") or {}
                break
            # Bedrock Converse metadata shape (future-proof)
            meta = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else None
            if meta and "usage" in meta:
                usage = meta["usage"] or {}
                break

    latency_ms = _as_int(metrics.get("latencyMs")) or None

    # 5m/1h cache split. Only Anthropic exposes it. For Converse, leave null → L2 prices as 5m.
    cache_creation = usage.get("cache_creation") if isinstance(usage, dict) else None
    if isinstance(cache_creation, dict):
        cache_write_5m = _as_int_or_none(cache_creation.get("ephemeral_5m_input_tokens"))
        cache_write_1h = _as_int_or_none(cache_creation.get("ephemeral_1h_input_tokens"))
    else:
        cache_write_5m = None
        cache_write_1h = None

    identity = record.get("identity") or {}
    caller = extract_caller(identity.get("arn") or "")

    error_code = record.get("errorCode") or None

    return {
        "account_id": account_id,
        "region": region,
        "request_id": request_id,
        "ts": timestamp_str,
        "operation": record.get("operation") or None,
        "model_id": model_id,
        "caller": caller or None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_5m_tokens": cache_write_5m,
        "cache_write_1h_tokens": cache_write_1h,
        "cache_write_total_tokens": cache_write_total,
        "latency_ms": latency_ms,
        "time_to_first_token_ms": None,  # not in log; CloudWatch only
        "error_code": error_code,
        "source_s3_key": source_s3_key,
        "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "schema_version": SCHEMA_VERSION,
    }


def _as_int(v):
    """Coerce to int with 0 default (for non-nullable BIGINT columns)."""
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _as_int_or_none(v):
    """Coerce to int, preserving null (for nullable BIGINT columns)."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract_caller(arn):
    """Extract readable caller name from IAM ARN."""
    if not arn:
        return ""
    # arn:aws:sts::123:assumed-role/RoleName/SessionName → RoleName/SessionName
    # arn:aws:iam::123:user/UserName → UserName
    parts = arn.split("/", 1)
    if len(parts) > 1:
        return parts[1]
    return arn.rsplit(":", 1)[-1]


def parse_ts(timestamp_str):
    """Validate timestamp parseable; return datetime or None."""
    if not timestamp_str:
        return None
    try:
        return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
