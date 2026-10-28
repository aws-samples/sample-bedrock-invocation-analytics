#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app entry point. Reads config.yaml to instantiate Hub and Spoke stacks."""

import boto3
import yaml
import aws_cdk as cdk

from hub_stack import HubStack, SPOKE_WRITE_ROLE_NAME
from spoke_stack import SpokeStack

with open("../config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

accounts = config.get("accounts", [])
primary = next((a for a in accounts if a.get("primary")), None)
if not primary:
    raise ValueError("No primary account defined in config.yaml")
spokes = [a for a in accounts if not a.get("primary")]
data_config = config.get("data") or {}
alarm_email = (config.get("alerts") or {}).get("email") or None

app = cdk.App()
target = app.node.try_get_context("target")  # "hub", "spoke:<profile>", "all"

if not target or target in ("hub", "all"):
    # The SpokeWriteRole trust policy is derived from config.yaml so it can't drift from the spoke list.
    spoke_account_ids = []
    for s in spokes:
        try:
            spoke_account_ids.append(
                boto3.Session(profile_name=s["profile"]).client("sts").get_caller_identity()["Account"])
        except Exception as e:  # noqa: BLE001 — an unreachable spoke profile shouldn't block a hub deploy
            print(f"[WARN] Skipping spoke {s.get('profile')}: can't resolve account_id ({e})")
    HubStack(app, "BedrockInvocationAnalytics",
        env=cdk.Environment(region=primary["region"]),
        cost_agg_interval_min=int(data_config.get("cost_agg_interval_min", 5)),
        spoke_accounts=spoke_account_ids,
        alarm_email=alarm_email,
    )

if target and (target.startswith("spoke:") or target == "all"):
    hub_account = app.node.try_get_context("hub_account") or ""
    target_profile = target.split(":", 1)[1] if target.startswith("spoke:") else None
    for s in spokes:
        if target_profile and s["profile"] != target_profile:
            continue
        SpokeStack(app, f"BedrockAnalytics-Spoke-{s['profile']}-{s['region']}",
            env=cdk.Environment(region=s["region"]),
            hub_role_arn=f"arn:aws:iam::{hub_account}:role/{SPOKE_WRITE_ROLE_NAME}",
            hub_firehose_name=app.node.try_get_context("hub_firehose_name") or "BedrockInvocationAnalytics-usage-events",
            hub_region=primary["region"],
            alarm_email=alarm_email,
        )

app.synth()
