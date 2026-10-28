# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK building blocks shared by the hub and spoke stacks."""

from aws_cdk import (
    CfnCondition,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
)
from constructs import Construct

LOG_RETENTION = logs.RetentionDays.ONE_MONTH
SPOKE_PARSE_LOG_ROLE_PREFIX = "BedrockAnalytics-SpokeParseLog-"


def python_function(scope: Construct, id: str, *, function_name: str, handler: str, asset: str = "lambda",
                    timeout_s: int = 60, memory_mb: int = 256, **kwargs) -> _lambda.Function:
    """ARM64 Python Lambda with its own log group (explicit retention, removed with the stack)."""
    log_group = logs.LogGroup(scope, f"{id}Logs", retention=LOG_RETENTION, removal_policy=RemovalPolicy.DESTROY)
    return _lambda.Function(
        scope, id,
        function_name=function_name,
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler=handler,
        code=_lambda.Code.from_asset(asset),
        timeout=Duration.seconds(timeout_s),
        memory_size=memory_mb,
        log_group=log_group,
        **kwargs,
    )


def ops_key(scope: Construct, id: str) -> kms.Key:
    """CMK for raw-log buckets and the alarm topic. Bedrock and CloudWatch alarms may use it."""
    stack = Stack.of(scope)
    key = kms.Key(scope, id, enable_key_rotation=True, removal_policy=RemovalPolicy.RETAIN,
                  description=f"{stack.stack_name}: Bedrock invocation logs and alarm notifications")
    key.add_to_resource_policy(iam.PolicyStatement(
        principals=[iam.ServicePrincipal("bedrock.amazonaws.com")],
        actions=["kms:GenerateDataKey"],
        resources=["*"],
        conditions={"StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{stack.region}:{stack.account}:*"}},
    ))
    key.add_to_resource_policy(iam.PolicyStatement(
        principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
        actions=["kms:GenerateDataKey*", "kms:Decrypt"],
        resources=["*"],
        conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
    ))
    return key


def tls_only_statement(bucket_arn: str) -> dict:
    return {
        "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [bucket_arn, f"{bucket_arn}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }


def raw_logs_bucket(scope: Construct, *, create: CfnCondition, key: kms.Key, log_prefix: str,
                    retention_days: int | None) -> s3.CfnBucket:
    """Conditionally created bucket for Bedrock invocation logs (full prompts and responses)."""
    stack = Stack.of(scope)
    lifecycle_rule = s3.CfnBucket.RuleProperty(
        id="TransitionAndExpire", status="Enabled", prefix=log_prefix,
        transitions=[s3.CfnBucket.TransitionProperty(storage_class="STANDARD_IA", transition_in_days=90)],
        expiration_in_days=retention_days,
    )
    bucket = s3.CfnBucket(
        scope, "LogsBucket",
        bucket_name=f"bedrock-logs-{stack.account}-{stack.region}",
        bucket_encryption=s3.CfnBucket.BucketEncryptionProperty(server_side_encryption_configuration=[
            s3.CfnBucket.ServerSideEncryptionRuleProperty(
                server_side_encryption_by_default=s3.CfnBucket.ServerSideEncryptionByDefaultProperty(
                    sse_algorithm="aws:kms", kms_master_key_id=key.key_arn),
                bucket_key_enabled=True,
            ),
        ]),
        ownership_controls=s3.CfnBucket.OwnershipControlsProperty(
            rules=[s3.CfnBucket.OwnershipControlsRuleProperty(object_ownership="BucketOwnerEnforced")]),
        public_access_block_configuration=s3.CfnBucket.PublicAccessBlockConfigurationProperty(
            block_public_acls=True, block_public_policy=True, ignore_public_acls=True, restrict_public_buckets=True),
        notification_configuration=s3.CfnBucket.NotificationConfigurationProperty(
            event_bridge_configuration=s3.CfnBucket.EventBridgeConfigurationProperty(event_bridge_enabled=True)),
        lifecycle_configuration=s3.CfnBucket.LifecycleConfigurationProperty(rules=[lifecycle_rule]),
    )
    bucket.cfn_options.condition = create
    bucket.apply_removal_policy(RemovalPolicy.RETAIN)

    bucket_arn = f"arn:aws:s3:::{bucket.ref}"
    policy = s3.CfnBucketPolicy(scope, "LogsBucketPolicy", bucket=bucket.ref, policy_document={
        "Version": "2012-10-17",
        "Statement": [
            tls_only_statement(bucket_arn),
            {
                "Sid": "AllowBedrockLogging", "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": Fn.join("", [bucket_arn, "/", log_prefix, "*"]),
                "Condition": {"StringEquals": {"aws:SourceAccount": stack.account},
                              "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{stack.region}:{stack.account}:*"}},
            },
        ],
    })
    policy.cfn_options.condition = create
    return bucket


def bedrock_invocation_logging(scope: Construct, *, bucket_name: str, log_prefix: str) -> CustomResource:
    fn = python_function(
        scope, "BedrockLoggingFunction",
        function_name=f"{Stack.of(scope).stack_name}-bedrock-invocation-setup",
        handler="bedrock_logging.handler", asset="custom_resources", timeout_s=60, memory_mb=128,
    )
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["bedrock:PutModelInvocationLoggingConfiguration", "bedrock:GetModelInvocationLoggingConfiguration",
                 "bedrock:DeleteModelInvocationLoggingConfiguration"],
        resources=["*"],  # account/region-level API without a resource ARN
    ))
    return CustomResource(scope, "BedrockLogging", service_token=fn.function_arn,
                          properties={"BucketName": bucket_name, "KeyPrefix": log_prefix, "HandlerVersion": "2"})


def read_raw_logs(fn: _lambda.Function, bucket_name: str, log_prefix: str):
    """Read access to Bedrock log objects, including SSE-KMS decrypt through S3."""
    stack = Stack.of(fn)
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["s3:GetObject"], resources=[f"arn:aws:s3:::{bucket_name}/{log_prefix}*"]))
    fn.add_to_role_policy(iam.PolicyStatement(
        actions=["kms:Decrypt"], resources=["*"],
        conditions={"StringEquals": {"kms:ViaService": f"s3.{stack.region}.amazonaws.com",
                                     "kms:CallerAccount": stack.account}},
    ))


def dead_letter_queue(scope: Construct, id: str, name: str) -> sqs.Queue:
    return sqs.Queue(scope, id, queue_name=name, retention_period=Duration.days(14),
                     encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True)


def alarm_topic(scope: Construct, key: kms.Key, email: str | None) -> sns.Topic:
    topic = sns.Topic(scope, "AlarmTopic", topic_name=f"{Stack.of(scope).stack_name}-alarms",
                      master_key=key, enforce_ssl=True)
    if email:
        topic.add_subscription(subs.EmailSubscription(email))
    return topic


class Alarms:
    """Creates alarms that all notify one topic."""

    def __init__(self, scope: Construct, topic: sns.Topic):
        self.scope, self.action = scope, cw_actions.SnsAction(topic)

    def add(self, id: str, metric: cw.IMetric, threshold: float, description: str,
            missing: cw.TreatMissingData = cw.TreatMissingData.NOT_BREACHING,
            evaluation_periods: int = 1) -> cw.Alarm:
        alarm = cw.Alarm(
            self.scope, id, metric=metric, threshold=threshold, evaluation_periods=evaluation_periods,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=missing, alarm_description=description,
        )
        alarm.add_alarm_action(self.action)
        alarm.add_ok_action(self.action)
        return alarm

    def function_errors(self, id: str, fn: _lambda.Function):
        self.add(id, fn.metric_errors(period=Duration.minutes(5), statistic="Sum"), 1,
                 f"{fn.function_name} reported errors")

    def dlq_depth(self, id: str, queue: sqs.Queue):
        self.add(id, queue.metric_approximate_number_of_messages_visible(period=Duration.minutes(5), statistic="Max"),
                 1, f"Messages waiting in {queue.queue_name}")
