# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Custom resource: pause on create/update so a new IAM role propagates before a dependent resource validates it."""

import time

import cfn


def handler(event, context):
    def handle(ev):
        if ev["RequestType"] != "Delete":
            time.sleep(int(ev["ResourceProperties"].get("Seconds", "30")))
        return ev.get("PhysicalResourceId") or "iam-propagation-wait"
    cfn.run(event, handle)
