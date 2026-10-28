#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app entry point. Reads config.yaml to instantiate Hub and Spoke stacks."""

import boto3
import yaml
import aws_cdk as cdk

from hub_stack import HubStack, SPOKE_WRITE_ROLE_NAME
from spoke_stack import SpokeStack
from webui import WebUiConfig

CLOUDFRONT_PREFIX_LIST = "com.amazonaws.global.cloudfront.origin-facing"

with open("../config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

accounts = config.get("accounts", [])
primary = next((a for a in accounts if a.get("primary")), None)
if not primary:
    raise ValueError("No primary account defined in config.yaml")
spokes = [a for a in accounts if not a.get("primary")]
data_config = config.get("data") or {}
alarm_email = (config.get("alerts") or {}).get("email") or None


def account_id(profile: str) -> str:
    return boto3.Session(profile_name=profile).client("sts").get_caller_identity()["Account"]


def webui_config(section: dict, account_names: dict[str, str]) -> WebUiConfig:
    """Validate the webui section and resolve what CDK can't look up on its own (subnet AZs, IGW, prefix list)."""
    unknown = set(section) - set(WebUiConfig.__dataclass_fields__) - {"subnet_ids"}
    if unknown:
        raise ValueError(f"Unknown webui settings in config.yaml: {sorted(unknown)} (admin_user/admin_pass were replaced by Cognito)")
    cfg = WebUiConfig(
        cloudfront=bool(section.get("cloudfront", False)),
        vpc_id=section.get("vpc_id") or "",
        allowed_cidrs=list(section.get("allowed_cidrs") or []),
        certificate_arn=section.get("certificate_arn") or "",
        cognito_user_pool_id=section.get("cognito_user_pool_id") or "",
        initial_admin_email=section.get("initial_admin_email") or "",
        admin_group=section.get("admin_group") or "",
        account_names=account_names,
    )
    if not cfg.cloudfront and not cfg.allowed_cidrs:
        raise ValueError("webui: set cloudfront: true or list allowed_cidrs, otherwise nobody can reach the WebUI")
    ec2 = boto3.Session(profile_name=primary["profile"], region_name=primary["region"]).client("ec2")
    if cfg.vpc_id:
        subnet_ids = list(section.get("subnet_ids") or [])
        if len(subnet_ids) < 2:
            raise ValueError("webui.subnet_ids: list at least two private subnets in different AZs of webui.vpc_id")
        subnets = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]
        if {s["VpcId"] for s in subnets} != {cfg.vpc_id}:
            raise ValueError("webui.subnet_ids must all belong to webui.vpc_id")
        cfg.subnets = [{"subnet_id": s["SubnetId"], "availability_zone": s["AvailabilityZone"]} for s in subnets]
        if len({s["availability_zone"] for s in cfg.subnets}) < 2:
            raise ValueError("webui.subnet_ids must span at least two availability zones")
        if cfg.cloudfront:
            igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [cfg.vpc_id]}])["InternetGateways"]
            if not igws:
                raise ValueError(f"webui.cloudfront needs an internet gateway on {cfg.vpc_id} (CloudFront VPC origin requirement)")
    if cfg.cognito_user_pool_id:
        pool_region = cfg.cognito_user_pool_id.split("_", 1)[0]
        idp = boto3.Session(profile_name=primary["profile"], region_name=pool_region).client("cognito-idp")
        try:
            idp.describe_user_pool(UserPoolId=cfg.cognito_user_pool_id)
        except idp.exceptions.ResourceNotFoundException as e:
            raise ValueError(f"webui.cognito_user_pool_id {cfg.cognito_user_pool_id} not found in the primary account ({pool_region})") from e
        if cfg.admin_group:
            try:
                idp.get_group(UserPoolId=cfg.cognito_user_pool_id, GroupName=cfg.admin_group)
            except idp.exceptions.ResourceNotFoundException as e:
                raise ValueError(f"webui.admin_group {cfg.admin_group!r} does not exist in {cfg.cognito_user_pool_id}") from e
        if cfg.initial_admin_email:
            print("[WARN] webui.initial_admin_email is ignored for an existing pool; add admins to the admin group yourself")
    if cfg.cloudfront:
        lists = ec2.describe_managed_prefix_lists(Filters=[{"Name": "prefix-list-name", "Values": [CLOUDFRONT_PREFIX_LIST]}])["PrefixLists"]
        cfg.cloudfront_prefix_list_id = lists[0]["PrefixListId"]
    if not cfg.certificate_arn and cfg.allowed_cidrs:
        print("[WARN] webui.certificate_arn is empty: intranet users reach the ALB over plaintext HTTP")
    return cfg


app = cdk.App()
target = app.node.try_get_context("target")  # "hub", "spoke:<profile>", "all"

if not target or target in ("hub", "all"):
    # The SpokeWriteRole trust policy is derived from config.yaml so it can't drift from the spoke list.
    spoke_account_ids, account_names = [], {}
    for a in accounts:
        try:
            acct = account_id(a["profile"])
        except Exception as e:  # noqa: BLE001 — an unreachable spoke profile shouldn't block a hub deploy
            print(f"[WARN] Skipping {a.get('profile')}: can't resolve account_id ({e})")
            continue
        account_names[acct] = a.get("name") or a["profile"]
        if not a.get("primary"):
            spoke_account_ids.append(acct)
    HubStack(app, "BedrockInvocationAnalytics",
        env=cdk.Environment(region=primary["region"]),
        cost_agg_interval_min=int(data_config.get("cost_agg_interval_min", 5)),
        spoke_accounts=spoke_account_ids,
        alarm_email=alarm_email,
        webui=webui_config(config.get("webui") or {}, account_names),
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
