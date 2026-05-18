#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app entry point. Reads config.yaml to instantiate Hub and Spoke stacks."""

import yaml
import aws_cdk as cdk
from hub_stack import HubStack
from spoke_stack import SpokeStack

with open("../config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

accounts = config.get("accounts", [])
primary = next((a for a in accounts if a.get("primary")), None)
if not primary:
    raise ValueError("No primary account defined in config.yaml")

spokes = [a for a in accounts if not a.get("primary")]
app = cdk.App()
target = app.node.try_get_context("target")  # "hub", "spoke:<profile>", "all"

# Hub stack — spoke_accounts come straight from config.yaml. Trust policies on the
# cross-account SpokeWriteRole derive from this list, so any spoke listed (and reachable
# via STS) gets baked in. Don't hand-pass it via context — that drifted us last time.
spoke_account_ids: list[str] = []
for s in spokes:
    try:
        import boto3
        spoke_account_ids.append(
            boto3.Session(profile_name=s["profile"]).client("sts").get_caller_identity()["Account"]
        )
    except Exception as e:
        print(f"[WARN] Skipping spoke {s.get('profile')}: can't resolve account_id ({e})")

data_config = config.get("data") or {}
if not target or target == "hub" or target == "all":
    HubStack(app, "BedrockInvocationAnalytics",
        env=cdk.Environment(region=primary["region"]),
        cost_agg_interval_min=int(data_config.get("cost_agg_interval_min", 5)),
        spoke_accounts=spoke_account_ids,
    )

# Spoke stacks
if target and (target.startswith("spoke:") or target == "all"):
    hub_account = app.node.try_get_context("hub_account") or ""
    hub_role_arn = f"arn:aws:iam::{hub_account}:role/BedrockAnalytics-SpokeWriteRole"
    hub_firehose_name = app.node.try_get_context("hub_firehose_name") or "BedrockInvocationAnalytics-usage-events"
    hub_region = primary["region"]

    target_profile = target.split(":")[1] if target.startswith("spoke:") else None
    for s in spokes:
        if target_profile and s["profile"] != target_profile:
            continue
        stack_id = f"BedrockAnalytics-Spoke-{s['profile']}-{s['region']}"
        SpokeStack(app, stack_id,
            env=cdk.Environment(region=s["region"]),
            hub_account=hub_account,
            hub_role_arn=hub_role_arn,
            hub_firehose_name=hub_firehose_name,
            hub_region=hub_region,
        )

app.synth()
