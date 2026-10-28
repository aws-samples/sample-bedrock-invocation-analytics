# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Custom resource: point Bedrock model invocation logging (account + region scoped) at the stack's S3 location.

Ownership is decided once, at create time, and stored in the PhysicalResourceId:
  created  no config existed; the stack owns it and deletes it on stack delete
  merged   a config without S3 delivery existed (e.g. CloudWatch only); the stack adds/removes only s3Config
  adopted  the config already delivered to this exact bucket/prefix; the stack never changes or deletes it
A config that already delivers to a different S3 location fails the deploy instead of being overwritten.
"""

import boto3

import cfn

ID_PREFIX = "bedrock-invocation-logging/"
CREATED, MERGED, ADOPTED = "created", "merged", "adopted"
# All modalities stay on: Bedrock only logs an invocation when its modality is enabled, and the
# analytics need every invocation. Text delivery also carries the usage block (cache split, latency).
DELIVERY_FLAGS = ("textDataDeliveryEnabled", "imageDataDeliveryEnabled",
                  "embeddingDataDeliveryEnabled", "videoDataDeliveryEnabled")


def handler(event, context):
    cfn.run(event, lambda ev: handle(ev, boto3.client("bedrock")))


def handle(event, client) -> str:
    props = event["ResourceProperties"]
    target = {"bucketName": props["BucketName"], "keyPrefix": props["KeyPrefix"]}
    old_id = event.get("PhysicalResourceId", "")
    mode = old_id.removeprefix(ID_PREFIX) if old_id.startswith(ID_PREFIX) else None
    current = _clean(client.get_model_invocation_logging_configuration().get("loggingConfig") or {})

    if event["RequestType"] == "Delete":
        if mode in (CREATED, MERGED) and (current.get("s3Config") or {}).get("bucketName") == target["bucketName"]:
            _remove_s3(client, current)
        return old_id or "none"

    if mode is None:  # Create, or Update of a resource created by an older handler version
        mode = decide_mode(current, target)
        new_config = desired_config(mode, current, target)
    elif mode == ADOPTED:
        if current.get("s3Config") != target:
            raise ValueError(f"Invocation logging is not owned by this stack and points to {current.get('s3Config')}; "
                             "change it outside the stack or match ExistingBucketName/LogPrefix")
        new_config = None
    else:
        new_config = {**current, "s3Config": target}

    if new_config is not None:
        client.put_model_invocation_logging_configuration(loggingConfig=new_config)
    return ID_PREFIX + mode


def decide_mode(current: dict, target: dict) -> str:
    s3 = current.get("s3Config")
    if not current:
        return CREATED
    if s3 == target:
        return ADOPTED
    if s3:
        raise ValueError(f"Invocation logging already delivers to s3://{s3.get('bucketName')}/{s3.get('keyPrefix', '')}; "
                         "set ExistingBucketName/LogPrefix to match it, or remove that configuration first")
    return MERGED


def desired_config(mode: str, current: dict, target: dict) -> dict | None:
    if mode == ADOPTED:
        return None
    if mode == CREATED:
        return {"s3Config": target, **{flag: True for flag in DELIVERY_FLAGS}}
    return {**current, "s3Config": target}


def _remove_s3(client, current: dict):
    remaining = {k: v for k, v in current.items() if k != "s3Config"}
    if remaining.get("cloudWatchConfig"):
        client.put_model_invocation_logging_configuration(loggingConfig=remaining)
    else:
        client.delete_model_invocation_logging_configuration()


def _clean(value):
    """Drop None values recursively so a fetched config can be sent back as-is."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    return value
