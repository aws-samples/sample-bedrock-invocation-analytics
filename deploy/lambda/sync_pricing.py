# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda: Sync Bedrock model pricing from LiteLLM.
Triggered weekly by EventBridge. Writes a new time-versioned record only when the price differs.

- A model's first record is backdated to INITIAL_EFFECTIVE_DATE so usage seen before the sync still prices.
- Price changes beyond MAX_CHANGE_RATIO (or to zero) are held back and reported instead of applied.
"""

import json
import math
import os
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
BEDROCK_PROVIDERS = ("bedrock", "bedrock_converse")
TOKEN_PRICED_MODES = ("chat", "completion", "responses", "embedding")
REQUEST_PRICED_MODES = ("rerank", "image_generation")
INITIAL_EFFECTIVE_DATE = "2000-01-01T00:00:00Z"
MAX_CHANGE_RATIO = 5.0
HTTP_TIMEOUT_S = 30
PRICE_FIELDS = ("input_per_1k", "output_per_1k", "cache_read_per_1k", "cache_write_per_1k",
                "cache_write_1h_per_1k", "per_request")


def extract_bedrock_prices(data: dict) -> dict[str, dict[str, str]]:
    """LiteLLM catalog → {model_id: {price field: decimal string}} for Bedrock models we can price."""
    out = {}
    for key, info in data.items():
        if not isinstance(info, dict) or "/" in key:  # "/" keys are size/step or region variants
            continue
        if not str(info.get("litellm_provider", "")).startswith(BEDROCK_PROVIDERS):
            continue
        mode = info.get("mode")
        per_token = {
            "input_per_1k": info.get("input_cost_per_token", 0) * 1000,
            "output_per_1k": info.get("output_cost_per_token", 0) * 1000,
            "cache_read_per_1k": info.get("cache_read_input_token_cost", 0) * 1000,
            "cache_write_per_1k": info.get("cache_creation_input_token_cost", 0) * 1000,
            "cache_write_1h_per_1k": info.get("cache_creation_input_token_cost_above_1hr", 0) * 1000,
        }
        # Image generation is priced per image; one image per request is assumed.
        per_request = info.get("input_cost_per_query") or info.get("output_cost_per_image") or 0
        if mode in TOKEN_PRICED_MODES and (per_token["input_per_1k"] or per_token["output_per_1k"]):
            prices = {**per_token, "per_request": 0}
        elif mode in REQUEST_PRICED_MODES and per_request:
            prices = {**{k: 0 for k in per_token}, "per_request": per_request}
        else:
            continue
        out[key] = {k: _fmt(v) for k, v in prices.items()}
    return out


def same_prices(current: dict, new: dict) -> bool:
    return all(math.isclose(float(current.get(f, 0) or 0), float(new.get(f, 0) or 0), rel_tol=1e-9, abs_tol=1e-12)
               for f in PRICE_FIELDS)


def is_suspicious_change(current: dict, new: dict) -> bool:
    """True when a non-zero price drops to zero or moves by more than MAX_CHANGE_RATIO."""
    for field in PRICE_FIELDS:
        old_v, new_v = float(current.get(field, 0) or 0), float(new.get(field, 0) or 0)
        if old_v and not new_v:
            return True
        if old_v and new_v and max(old_v / new_v, new_v / old_v) > MAX_CHANGE_RATIO:
            return True
    return False


def handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    pricing_table = dynamodb.Table(os.environ["MODEL_PRICING_TABLE"])
    usage_table_name = os.environ.get("USAGE_STATS_TABLE", "")

    with urllib.request.urlopen(LITELLM_URL, timeout=HTTP_TIMEOUT_S) as resp:
        prices = extract_bedrock_prices(json.loads(resp.read()))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated, added, skipped, held = 0, 0, 0, []

    for model_id, new in prices.items():
        items = pricing_table.query(
            KeyConditionExpression=Key("PK").eq(f"MODEL#{model_id}") & Key("SK").lte(now),
            ScanIndexForward=False, Limit=1,
        ).get("Items", [])
        if items:
            current = items[0]
            if current.get("price_as"):  # priced as another model by an admin; never overwrite
                skipped += 1
                continue
            if same_prices(current, new):
                skipped += 1
                continue
            if is_suspicious_change(current, new):
                held.append(model_id)
                continue
            sk = now
            updated += 1
        else:
            sk = INITIAL_EFFECTIVE_DATE
            added += 1
        pricing_table.put_item(Item={"PK": f"MODEL#{model_id}", "SK": sk, **new, "source": "litellm"})

    if usage_table_name:
        dynamodb.Table(usage_table_name).put_item(Item={
            "PK": "META", "SK": "PRICING_SYNC#latest",
            "synced_at": now, "models_updated": updated, "models_added": added,
            "models_skipped": skipped, "models_held": held[:50],
        })

    result = {"added": added, "updated": updated, "skipped": skipped, "held": held}
    print(json.dumps(result))
    return result


def _fmt(v: float) -> str:
    """Decimal string without float noise; '0' for zero (matches stored records)."""
    return format(round(v, 9), "f").rstrip("0").rstrip(".") or "0"
