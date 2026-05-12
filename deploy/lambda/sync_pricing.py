# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda: Sync Bedrock model pricing from LiteLLM.
Triggered weekly by EventBridge. Compares current effective price with LiteLLM;
writes new record only when price differs.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

PRICING_TABLE = os.environ["MODEL_PRICING_TABLE"]
USAGE_TABLE = os.environ.get("USAGE_STATS_TABLE", "")
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
BEDROCK_PROVIDERS = ("bedrock", "bedrock_converse")

dynamodb = boto3.resource("dynamodb")
pricing_table = dynamodb.Table(PRICING_TABLE)


def handler(event, context):
    print("Fetching LiteLLM pricing data...")
    with urllib.request.urlopen(LITELLM_URL) as resp:
        data = json.loads(resp.read())

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated, skipped = 0, 0

    for key, info in data.items():
        provider = info.get("litellm_provider", "")
        if not any(provider.startswith(p) for p in BEDROCK_PROVIDERS):
            continue
        if info.get("mode") != "chat":
            continue
        input_cost = info.get("input_cost_per_token", 0)
        output_cost = info.get("output_cost_per_token", 0)
        if not input_cost and not output_cost:
            continue

        new_input = str(round(input_cost * 1000, 6))
        new_output = str(round(output_cost * 1000, 6))
        new_cache_read = str(round(info.get("cache_read_input_token_cost", 0) * 1000, 6))
        new_cache_write = str(round(info.get("cache_creation_input_token_cost", 0) * 1000, 6))
        new_cache_write_1h = str(round(info.get("cache_creation_input_token_cost_above_1hr", 0) * 1000, 6))

        # Get current effective price
        resp = pricing_table.query(
            KeyConditionExpression=Key("PK").eq(f"MODEL#{key}") & Key("SK").lte(now),
            ScanIndexForward=False, Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            cur = items[0]
            if (cur.get("input_per_1k") == new_input and cur.get("output_per_1k") == new_output
                    and cur.get("cache_read_per_1k") == new_cache_read and cur.get("cache_write_per_1k") == new_cache_write
                    and cur.get("cache_write_1h_per_1k") == new_cache_write_1h):
                skipped += 1
                continue

        pricing_table.put_item(Item={
            "PK": f"MODEL#{key}",
            "SK": now,
            "input_per_1k": new_input,
            "output_per_1k": new_output,
            "cache_read_per_1k": new_cache_read,
            "cache_write_per_1k": new_cache_write,
            "cache_write_1h_per_1k": new_cache_write_1h,
            "source": "litellm",
        })
        updated += 1

    # Record sync metadata
    if USAGE_TABLE:
        dynamodb.Table(USAGE_TABLE).put_item(Item={
            "PK": "META", "SK": "PRICING_SYNC#latest",
            "synced_at": now, "models_updated": updated, "models_skipped": skipped,
        })

    print(f"Sync complete: {updated} updated, {skipped} unchanged")
    return {"updated": updated, "skipped": skipped}
