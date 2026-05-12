# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Seed DynamoDB pricing table from LiteLLM model_prices JSON."""

import json
import sys
import urllib.request
from datetime import datetime, timezone

import boto3

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
BEDROCK_PROVIDERS = ("bedrock", "bedrock_converse")
DEFAULT_TABLE = "BedrockInvocationAnalytics-model-pricing"
DEFAULT_EFFECTIVE_DATE = "2025-03-01T00:00:00Z"


def fetch_pricing():
    with urllib.request.urlopen(LITELLM_URL) as resp:
        return json.loads(resp.read())


def extract_bedrock_models(data):
    models = {}
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

        # Use the key as model_id (strip bedrock/ prefix if present)
        model_id = key
        # Convert per-token to per-1k-token
        models[model_id] = {
            "input_per_1k": round(input_cost * 1000, 6),
            "output_per_1k": round(output_cost * 1000, 6),
            "cache_read_per_1k": round(info.get("cache_read_input_token_cost", 0) * 1000, 6),
            "cache_write_per_1k": round(info.get("cache_creation_input_token_cost", 0) * 1000, 6),
            "cache_write_1h_per_1k": round(info.get("cache_creation_input_token_cost_above_1hr", 0) * 1000, 6),
        }
    return models


def seed_table(table_name, models, profile=None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    table = session.resource("dynamodb").Table(table_name)
    effctive_date = DEFAULT_EFFECTIVE_DATE

    with table.batch_writer() as batch:
        for model_id, pricing in models.items():
            batch.put_item(Item={
                "PK": f"MODEL#{model_id}",
                "SK": effctive_date,
                "input_per_1k": str(pricing["input_per_1k"]),
                "output_per_1k": str(pricing["output_per_1k"]),
                "cache_read_per_1k": str(pricing["cache_read_per_1k"]),
                "cache_write_per_1k": str(pricing["cache_write_per_1k"]),
                "cache_write_1h_per_1k": str(pricing["cache_write_1h_per_1k"]),
                "source": "litellm",
            })
    print(f"Seeded {len(models)} models to {table_name}")


if __name__ == "__main__":
    table = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TABLE
    profile = sys.argv[2] if len(sys.argv) > 2 else None

    print("Fetching LiteLLM pricing data...")
    data = fetch_pricing()
    models = extract_bedrock_models(data)
    print(f"Found {len(models)} Bedrock chat models")

    seed_table(table, models, profile)
