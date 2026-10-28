# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Minimal CloudFormation custom-resource response helper (stdlib only)."""

import json
import urllib.request


def send(event, status: str, physical_id: str, reason: str = "", data: dict | None = None):
    body = json.dumps({
        "Status": status,
        "Reason": reason[:1000] or "See CloudWatch Logs",
        "PhysicalResourceId": physical_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                 headers={"content-type": "", "content-length": str(len(body))})
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"CFN response {status}: HTTP {resp.status}")


def run(event, handle):
    """Call handle(event) -> physical_id and report SUCCESS, or FAILED with the error message."""
    print(json.dumps({k: v for k, v in event.items() if k != "ResponseURL"}))
    try:
        send(event, "SUCCESS", handle(event))
    except Exception as e:  # noqa: BLE001 — every failure must reach CloudFormation or the stack hangs
        print(f"{type(e).__name__}: {e}")
        send(event, "FAILED", event.get("PhysicalResourceId") or "failed", str(e))
