# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK Spoke Stack: Bedrock logging to a local bucket + parse_log forwarding events to the hub Firehose."""

from aws_cdk import (
    CfnCondition, CfnOutput, CfnParameter, Duration, Fn, Stack,
    aws_events as events, aws_events_targets as targets, aws_iam as iam,
)
from constructs import Construct

import common


class SpokeStack(Stack):
    def __init__(self, scope: Construct, id: str, *, hub_role_arn: str, hub_firehose_name: str, hub_region: str,
                 alarm_email: str | None = None, **kwargs):
        super().__init__(scope, id, **kwargs)

        existing_bucket_name = CfnParameter(self, "ExistingBucketName", type="String", default="",
            description="Leave empty to create a new bucket, or specify an existing bucket name.")
        log_prefix = CfnParameter(self, "LogPrefix", type="String", default="bedrock/invocation-logs/",
            description="S3 key prefix for invocation logs.").value_as_string

        create_bucket = CfnCondition(self, "CreateNewBucket",
            expression=Fn.condition_equals(existing_bucket_name.value_as_string, ""))
        key = common.ops_key(self, "OpsKey")
        logs_bucket = common.raw_logs_bucket(self, create=create_bucket, key=key, log_prefix=log_prefix,
                                             retention_days=None)
        bucket_name = Fn.condition_if(create_bucket.logical_id, logs_bucket.ref,
                                      existing_bucket_name.value_as_string).to_string()

        common.bedrock_invocation_logging(self, bucket_name=bucket_name, log_prefix=log_prefix)

        dlq = common.dead_letter_queue(self, "ProcessLogDLQ", f"{id}-parse-log-dlq")
        # Fixed name: the hub's SpokeWriteRole trust policy matches it by aws:PrincipalArn.
        role = iam.Role(self, "ParseLogRole",
            role_name=f"{common.SPOKE_PARSE_LOG_ROLE_PREFIX}{self.region}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        parse_log_fn = common.python_function(self, "ParseLogFunction",
            function_name=f"{id}-parse-log", handler="parse_log.handler", role=role,
            dead_letter_queue=dlq, retry_attempts=2,
            environment={"FIREHOSE_STREAM": hub_firehose_name, "HUB_ROLE_ARN": hub_role_arn,
                         "HUB_REGION": hub_region or self.region},
        )
        common.read_raw_logs(parse_log_fn, bucket_name, log_prefix)
        parse_log_fn.add_to_role_policy(iam.PolicyStatement(actions=["sts:AssumeRole"], resources=[hub_role_arn]))

        events.Rule(self, "NewLogFileTrigger",
            rule_name=f"{id}-new-log-trigger",
            event_pattern=events.EventPattern(
                source=["aws.s3"], detail_type=["Object Created"],
                detail={"bucket": {"name": [bucket_name]}, "object": {"key": [{"suffix": ".json.gz"}]}},
            ),
            targets=[targets.LambdaFunction(parse_log_fn, dead_letter_queue=dlq,
                                            retry_attempts=8, max_event_age=Duration.hours(6))],
        )

        alarms = common.Alarms(self, common.alarm_topic(self, key, alarm_email))
        alarms.function_errors("ParseLogErrorsAlarm", parse_log_fn)
        alarms.dlq_depth("ParseLogDLQAlarm", dlq)

        CfnOutput(self, "BucketName", value=bucket_name)
        CfnOutput(self, "ParseLogFunctionName", value=parse_log_fn.function_name)
