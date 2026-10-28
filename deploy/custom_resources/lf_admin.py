# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Custom resource: ensure one principal is a Lake Formation data lake admin without replacing the admin list.

PutDataLakeSettings replaces the whole settings object, so this reads, adds the principal, and writes back.
The PhysicalResourceId records whether the stack added the principal; only then does Delete remove it.
"""

import boto3

import cfn

ID_PREFIX = "lf-admin/"
ADDED, PREEXISTING = "added", "preexisting"


def handler(event, context):
    cfn.run(event, lambda ev: handle(ev, boto3.client("lakeformation")))


def handle(event, client) -> str:
    principal = event["ResourceProperties"]["Principal"]
    old_id = event.get("PhysicalResourceId", "")
    settings = client.get_data_lake_settings()["DataLakeSettings"]
    admins = settings.get("DataLakeAdmins", [])
    present = any(a.get("DataLakePrincipalIdentifier") == principal for a in admins)

    if event["RequestType"] == "Delete":
        if old_id == f"{ID_PREFIX}{ADDED}/{principal}" and present:
            settings["DataLakeAdmins"] = [a for a in admins if a.get("DataLakePrincipalIdentifier") != principal]
            client.put_data_lake_settings(DataLakeSettings=settings)
        return old_id or "none"

    if old_id.startswith(f"{ID_PREFIX}{ADDED}/{principal}") or old_id.startswith(f"{ID_PREFIX}{PREEXISTING}/{principal}"):
        if not present:
            settings["DataLakeAdmins"] = admins + [{"DataLakePrincipalIdentifier": principal}]
            client.put_data_lake_settings(DataLakeSettings=settings)
        return old_id
    if present:
        return f"{ID_PREFIX}{PREEXISTING}/{principal}"
    settings["DataLakeAdmins"] = admins + [{"DataLakePrincipalIdentifier": principal}]
    client.put_data_lake_settings(DataLakeSettings=settings)
    return f"{ID_PREFIX}{ADDED}/{principal}"
