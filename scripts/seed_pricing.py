# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Seed the pricing table from LiteLLM. Usage: seed_pricing.py [TABLE] [PROFILE]"""

import json
import sys
import urllib.request
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy" / "lambda"))
from sync_pricing import HTTP_TIMEOUT_S, INITIAL_EFFECTIVE_DATE, LITELLM_URL, extract_bedrock_prices  # noqa: E402

DEFAULT_TABLE = "BedrockInvocationAnalytics-model-pricing"


def main():
    table_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TABLE
    profile = sys.argv[2] if len(sys.argv) > 2 else None

    with urllib.request.urlopen(LITELLM_URL, timeout=HTTP_TIMEOUT_S) as resp:
        prices = extract_bedrock_prices(json.loads(resp.read()))

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    table = session.resource("dynamodb").Table(table_name)
    with table.batch_writer() as batch:
        for model_id, fields in prices.items():
            batch.put_item(Item={"PK": f"MODEL#{model_id}", "SK": INITIAL_EFFECTIVE_DATE, **fields, "source": "litellm"})
    print(f"Seeded {len(prices)} models to {table_name}")


if __name__ == "__main__":
    main()
