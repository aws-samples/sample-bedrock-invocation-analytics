# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK Hub Stack for Bedrock Invocation Analytics."""

from aws_cdk import (
    CfnCondition,
    CfnOutput,
    CfnParameter,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    aws_athena as athena,
    aws_cloudwatch as cw,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_glue as glue,
    aws_iam as iam,
    aws_kinesisfirehose as firehose,
    aws_lambda_destinations as lambda_destinations,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3tables as s3tables,
)
from constructs import Construct

import common
from webui import WebUi, WebUiConfig

ICEBERG_NAMESPACE = "bedrock_analytics"
ICEBERG_TABLE = "usage_events"
SPOKE_WRITE_ROLE_NAME = "BedrockAnalytics-SpokeWriteRole"

# (field id, name, type, required) — S3 Tables requires lowercase names.
USAGE_EVENTS_SCHEMA = [
    (1, "account_id", "string", True),
    (2, "region", "string", True),
    (3, "request_id", "string", True),
    (4, "ts", "timestamptz", True),
    (5, "operation", "string", False),
    (6, "model_id", "string", False),
    (7, "caller", "string", False),
    (8, "input_tokens", "long", False),
    (9, "output_tokens", "long", False),
    (10, "cache_read_tokens", "long", False),
    (11, "cache_write_5m_tokens", "long", False),
    (12, "cache_write_1h_tokens", "long", False),
    (13, "cache_write_total_tokens", "long", False),
    (14, "latency_ms", "int", False),
    (15, "time_to_first_token_ms", "int", False),
    (16, "error_code", "string", False),
    (17, "source_s3_key", "string", False),
    (18, "parsed_at", "timestamptz", False),
    (19, "schema_version", "int", False),
]


SETTLE_MINUTE = 20  # minute past each hour when settle runs; reprice chunks keep clear of it

class HubStack(Stack):

    def __init__(self, scope: Construct, id: str, *, cost_agg_interval_min: int = 5,
                 spoke_accounts: list[str] | None = None, alarm_email: str | None = None,
                 webui: WebUiConfig | None = None, **kwargs):
        super().__init__(scope, id, **kwargs)
        if cost_agg_interval_min < 2:
            raise ValueError("cost_agg_interval_min must be >= 2 (Firehose buffers for 60s)")

        existing_bucket_name = CfnParameter(self, "ExistingBucketName", type="String", default="",
            description="Leave empty to create a new bucket, or specify an existing bucket name.")
        log_prefix = CfnParameter(self, "LogPrefix", type="String", default="bedrock/invocation-logs/",
            description="S3 key prefix for invocation logs.").value_as_string
        log_retention_days = CfnParameter(self, "LogRetentionDays", type="Number", default=365,
            description="Days before logs expire (only for newly created bucket).")

        create_bucket = CfnCondition(self, "CreateNewBucket",
            expression=Fn.condition_equals(existing_bucket_name.value_as_string, ""))
        key = common.ops_key(self, "OpsKey")
        logs_bucket = common.raw_logs_bucket(self, create=create_bucket, key=key, log_prefix=log_prefix,
                                             retention_days=log_retention_days.value_as_number)
        bucket_name = Fn.condition_if(create_bucket.logical_id, logs_bucket.ref,
                                      existing_bucket_name.value_as_string).to_string()

        common.bedrock_invocation_logging(self, bucket_name=bucket_name, log_prefix=log_prefix)

        # Pipeline scratch space: Athena results and Firehose error backups, kept off the raw-prompt bucket.
        ops_bucket = s3.Bucket(self, "OpsBucket",
            bucket_name=f"bedrock-analytics-ops-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(id="AthenaResults", prefix="athena-results/", expiration=Duration.days(7)),
                s3.LifecycleRule(id="FirehoseErrors", prefix="firehose-errors/", expiration=Duration.days(90)),
                s3.LifecycleRule(id="AbortUploads", abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        usage_stats_table = self._table("UsageStatsTable", f"{id}-usage-stats", ttl="ttl")
        model_pricing_table = self._table("ModelPricingTable", f"{id}-model-pricing")

        catalog = self._iceberg_catalog()
        stream = self._firehose(id, catalog, ops_bucket)

        alarms = common.Alarms(self, common.alarm_topic(self, key, alarm_email))

        # ── L1: parse_log (S3 event → Firehose) ──
        parse_dlq = common.dead_letter_queue(self, "ParseLogDLQ", f"{id}-parse-log-dlq")
        parse_log_fn = common.python_function(self, "ParseLogFunction",
            function_name=f"{id}-parse-log", handler="parse_log.handler",
            dead_letter_queue=parse_dlq, retry_attempts=2,
            environment={"FIREHOSE_STREAM": stream.delivery_stream_name or "", "HUB_REGION": self.region},
        )
        common.read_raw_logs(parse_log_fn, bucket_name, log_prefix)
        parse_log_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["firehose:PutRecord", "firehose:PutRecordBatch"], resources=[stream.attr_arn]))
        events.Rule(self, "NewLogFileTriggerV3",
            rule_name=f"{id}-new-log-trigger-v3",
            event_pattern=events.EventPattern(
                source=["aws.s3"], detail_type=["Object Created"],
                detail={"bucket": {"name": [bucket_name]}, "object": {"key": [{"suffix": ".json.gz"}]}},
            ),
            targets=[targets.LambdaFunction(parse_log_fn, dead_letter_queue=parse_dlq,
                                            retry_attempts=8, max_event_age=Duration.hours(6))],
        )

        # ── L2: compute_cost (Iceberg → priced aggregates in DynamoDB) ──
        workgroup = athena.CfnWorkGroup(self, "AthenaWorkgroup",
            name=f"{id}-compute", state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{ops_bucket.bucket_name}/athena-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(encryption_option="SSE_S3"),
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=10 * 1024 ** 3,
            ),
        )
        # Failed async invocations (incremental incl. reprice chunks, settle) land here; all resume on the next run.
        # Purge after review.
        compute_failures = common.dead_letter_queue(self, "ComputeCostFailures", f"{id}-compute-cost-failures")
        compute_cost_fn = common.python_function(self, "ComputeCostFunction",
            function_name=f"{id}-compute-cost", handler="compute_cost.handler", timeout_s=600, memory_mb=1024,
            retry_attempts=0, on_failure=lambda_destinations.SqsDestination(compute_failures),
            environment=(compute_env := {
                "STACK_NAME": id,
                "SETTLE_MINUTE": str(SETTLE_MINUTE),
                "USAGE_STATS_TABLE": usage_stats_table.table_name,
                "MODEL_PRICING_TABLE": model_pricing_table.table_name,
                "ATHENA_WORKGROUP": workgroup.name,
                "ICEBERG_CATALOG": f"s3tablescatalog/{catalog['bucket_name']}",
                "ICEBERG_DATABASE": ICEBERG_NAMESPACE,
                "ICEBERG_TABLE": ICEBERG_TABLE,
            }),
        )
        # The WebUI invokes this to start a reprice job now: one incremental run whatever the payload. No retries and no
        # failure destination (errors are in the job record and its own alarm); the L2 lease serializes the runs.
        reprice_tick_fn = common.python_function(self, "RepriceTickFunction",
            function_name=f"{id}-compute-cost-tick", handler="compute_cost.tick_handler", timeout_s=600, memory_mb=1024,
            role=compute_cost_fn.role, retry_attempts=0, max_event_age=Duration.seconds(60), environment=compute_env)
        usage_stats_table.grant_read_write_data(compute_cost_fn)
        model_pricing_table.grant_read_data(compute_cost_fn)
        self._grant_athena_read(compute_cost_fn, workgroup, ops_bucket, catalog)
        events.Rule(self, "ComputeCostSchedule",
            rule_name=f"{id}-compute-cost-schedule",
            schedule=events.Schedule.rate(Duration.minutes(cost_agg_interval_min)),
            targets=[targets.LambdaFunction(compute_cost_fn, retry_attempts=0)],
        )

        # Settle: recompute dirty and recently completed days (HOURLY + DAILY) and completed months from Iceberg.
        events.Rule(self, "SettleSchedule", rule_name=f"{id}-settle",
            schedule=events.Schedule.cron(minute=str(SETTLE_MINUTE)),
            targets=[targets.LambdaFunction(compute_cost_fn, event=events.RuleTargetInput.from_object({"settle": {}}))])

        # ── Pricing sync ──
        sync_pricing_fn = common.python_function(self, "SyncPricingFunction",
            function_name=f"{id}-sync-pricing", handler="sync_pricing.handler", timeout_s=120,
            environment={"MODEL_PRICING_TABLE": model_pricing_table.table_name,
                         "USAGE_STATS_TABLE": usage_stats_table.table_name})
        model_pricing_table.grant_read_write_data(sync_pricing_fn)
        usage_stats_table.grant_write_data(sync_pricing_fn)
        events.Rule(self, "WeeklyPricingSyncSchedule", rule_name=f"{id}-weekly-pricing-sync",
            schedule=events.Schedule.cron(minute="0", hour="21", week_day="SUN"),
            targets=[targets.LambdaFunction(sync_pricing_fn)])

        # ── Cross-account: spokes put records on the hub stream, nothing else ──
        if spoke_accounts:
            spoke_role = iam.Role(self, "SpokeWriteRole",
                role_name=SPOKE_WRITE_ROLE_NAME,
                assumed_by=iam.CompositePrincipal(*[
                    iam.AccountPrincipal(a).with_conditions({"ArnLike": {
                        "aws:PrincipalArn": f"arn:aws:iam::{a}:role/{common.SPOKE_PARSE_LOG_ROLE_PREFIX}*"}})
                    for a in spoke_accounts]),
                max_session_duration=Duration.hours(1),
            )
            spoke_role.add_to_policy(iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"], resources=[stream.attr_arn]))
            CfnOutput(self, "SpokeWriteRoleArn", value=spoke_role.role_arn)

        # ── Alarms ──
        for name, fn in (("ParseLog", parse_log_fn), ("ComputeCost", compute_cost_fn), ("RepriceTick", reprice_tick_fn),
                         ("SyncPricing", sync_pricing_fn)):
            alarms.function_errors(f"{name}ErrorsAlarm", fn)
        alarms.dlq_depth("ParseLogDLQAlarm", parse_dlq)
        firehose_metric = lambda name, stat: cw.Metric(  # noqa: E731
            namespace="AWS/Firehose", metric_name=name, statistic=stat, period=Duration.minutes(5),
            dimensions_map={"DeliveryStreamName": stream.delivery_stream_name or ""})
        alarms.add("FirehoseFailedRowsAlarm", firehose_metric("DeliveryToIceberg.FailedRowCount", "Sum"), 1,
                   "Firehose sent rows to the error backup prefix instead of Iceberg")
        alarms.add("FirehoseFreshnessAlarm", firehose_metric("DeliveryToIceberg.DataFreshness", "Maximum"), 900,
                   "Oldest undelivered Firehose record is older than 15 minutes. Events visible more than 15 min after "
                   "ingestion are picked up by settle for the last 2 days; run scripts/rebuild_costs.sh for older days")
        l2_metric = lambda name, stat, minutes: cw.Metric(  # noqa: E731
            namespace="BedrockAnalytics", metric_name=name, statistic=stat, period=Duration.minutes(minutes),
            dimensions_map={"Stack": id})
        alarms.add("L2LagAlarm", l2_metric("CheckpointLagSeconds", "Maximum", 15), 1800,
                   "L2 checkpoint is more than 30 minutes behind, or L2 stopped reporting",
                   missing=cw.TreatMissingData.BREACHING)
        alarms.dlq_depth("ComputeCostFailuresAlarm", compute_failures)
        alarms.add("SettleLagAlarm", l2_metric("SettleLagHours", "Maximum", 60), 25,
                   "Yesterday is not settled into DAILY 1-2 hours after midnight UTC, or settle stopped reporting",
                   missing=cw.TreatMissingData.BREACHING)
        alarms.add("UnpricedEventsAlarm", l2_metric("UnpricedEvents", "Sum", 60), 1,
                   "Invocations of models without a price; add pricing, then run scripts/rebuild_costs.sh")

        if webui:
            WebUi(self, "WebUi", config=webui, usage_table=usage_stats_table, pricing_table=model_pricing_table,
                  reprice_tick_fn=reprice_tick_fn,
                  data_sources={"athena_workgroup": workgroup.name,
                                "iceberg_table": f"\"s3tablescatalog/{catalog['bucket_name']}\".{ICEBERG_NAMESPACE}.{ICEBERG_TABLE}"})

        CfnOutput(self, "BucketName", value=bucket_name)
        CfnOutput(self, "OpsBucketName", value=ops_bucket.bucket_name)
        CfnOutput(self, "UsageStatsTableName", value=usage_stats_table.table_name)
        CfnOutput(self, "ModelPricingTableName", value=model_pricing_table.table_name)
        CfnOutput(self, "UsageTableBucketArn", value=catalog["bucket_arn"])
        CfnOutput(self, "UsageEventsFirehoseName", value=stream.delivery_stream_name or "")
        CfnOutput(self, "UsageEventsFirehoseArn", value=stream.attr_arn)
        CfnOutput(self, "ParseLogFunctionName", value=parse_log_fn.function_name)
        CfnOutput(self, "ComputeCostFunctionName", value=compute_cost_fn.function_name)
        CfnOutput(self, "AthenaWorkgroupName", value=workgroup.name)

    def _table(self, id: str, name: str, ttl: str | None = None) -> dynamodb.Table:
        return dynamodb.Table(self, id,
            table_name=name,
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute=ttl,
        )

    def _iceberg_catalog(self) -> dict:
        """S3 Tables bucket + Iceberg table, exposed to Athena/Firehose through the Glue federated catalog."""
        bucket_name = f"bedrock-analytics-{self.account}"
        bucket_arn = f"arn:aws:s3tables:{self.region}:{self.account}:bucket/{bucket_name}"

        # The CDK deploy role must be a Lake Formation admin to create the federated catalog.
        lf_fn = common.python_function(self, "LFAdminFunction", function_name=f"{self.stack_name}-lf-admin",
            handler="lf_admin.handler", asset="custom_resources", memory_mb=128)
        lf_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["lakeformation:GetDataLakeSettings", "lakeformation:PutDataLakeSettings"], resources=["*"]))
        lf_admin = CustomResource(self, "CdkDeployRoleLFAdmin", service_token=lf_fn.function_arn, properties={
            "Principal": f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-cfn-exec-role-{self.account}-{self.region}"})

        table_bucket = s3tables.CfnTableBucket(self, "UsageTableBucket", table_bucket_name=bucket_name)
        table_bucket.apply_removal_policy(RemovalPolicy.RETAIN)
        namespace = s3tables.CfnNamespace(self, "IcebergNamespace", namespace=ICEBERG_NAMESPACE, table_bucket_arn=bucket_arn)
        namespace.add_dependency(table_bucket)
        namespace.apply_removal_policy(RemovalPolicy.RETAIN)

        table = s3tables.CfnTable(self, "UsageEventsTable",
            table_bucket_arn=bucket_arn, namespace=ICEBERG_NAMESPACE, table_name=ICEBERG_TABLE,
            open_table_format="ICEBERG",
            iceberg_metadata=s3tables.CfnTable.IcebergMetadataProperty(
                iceberg_schema=s3tables.CfnTable.IcebergSchemaProperty(schema_field_list=[
                    s3tables.CfnTable.SchemaFieldProperty(name=n, type=t, required=r) for (_, n, t, r) in USAGE_EVENTS_SCHEMA]),
                # day(ts) alone: Iceberg rejects DML on specs that also carry year/month of the same column.
                iceberg_partition_spec=s3tables.CfnTable.IcebergPartitionSpecProperty(fields=[
                    s3tables.CfnTable.IcebergPartitionFieldProperty(source_id=1, transform="identity", name="account_id"),
                    s3tables.CfnTable.IcebergPartitionFieldProperty(source_id=4, transform="day", name="ts_day"),
                ]),
            ),
        )
        table.add_dependency(namespace)
        table.apply_removal_policy(RemovalPolicy.RETAIN)

        iam_allowed = glue.CfnCatalog.PrincipalPermissionsProperty(
            principal=glue.CfnCatalog.DataLakePrincipalProperty(data_lake_principal_identifier="IAM_ALLOWED_PRINCIPALS"),
            permissions=["ALL"])
        glue_catalog = glue.CfnCatalog(self, "GlueS3TablesCatalog",
            name="s3tablescatalog",
            federated_catalog=glue.CfnCatalog.FederatedCatalogProperty(
                identifier=f"arn:aws:s3tables:{self.region}:{self.account}:bucket/*", connection_name="aws:s3tables"),
            create_database_default_permissions=[iam_allowed],
            create_table_default_permissions=[iam_allowed],
            allow_full_table_external_data_access="True",
        )
        glue_catalog.add_dependency(table_bucket)
        glue_catalog.node.add_dependency(lf_admin)
        glue_catalog.apply_removal_policy(RemovalPolicy.RETAIN)
        return {"bucket_name": bucket_name, "bucket_arn": bucket_arn, "table": table, "glue_catalog": glue_catalog}

    def _firehose(self, id: str, catalog: dict, ops_bucket: s3.Bucket) -> firehose.CfnDeliveryStream:
        """DirectPut stream into the Iceberg table. Records carry no operation, so writes are appends even though
        unique_keys is set; readers deduplicate by request_id."""
        log_group = logs.LogGroup(self, "FirehoseLogGroup", log_group_name=f"/aws/kinesisfirehose/{id}-usage-events",
            retention=common.LOG_RETENTION, removal_policy=RemovalPolicy.DESTROY)
        log_stream = logs.LogStream(self, "FirehoseLogStream", log_group=log_group,
            log_stream_name="IcebergDelivery", removal_policy=RemovalPolicy.DESTROY)

        role = iam.Role(self, "FirehoseRole", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com", conditions={
            "StringEquals": {"aws:SourceAccount": self.account}}))
        glue_arns = [f"arn:aws:glue:{self.region}:{self.account}:{r}" for r in (
            "catalog", "catalog/s3tablescatalog", "catalog/s3tablescatalog/*", "database/*", "table/*/*")]
        policy = iam.Policy(self, "FirehoseRolePolicy", roles=[role], statements=[
            iam.PolicyStatement(actions=["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables",
                                         "glue:UpdateTable"], resources=glue_arns),
            iam.PolicyStatement(actions=["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:GetTable",
                                         "s3tables:GetTableData", "s3tables:GetTableMetadataLocation",
                                         "s3tables:PutTableData", "s3tables:UpdateTableMetadataLocation"],
                                resources=[catalog["bucket_arn"], f"{catalog['bucket_arn']}/table/*"]),
            iam.PolicyStatement(actions=["lakeformation:GetDataAccess"], resources=["*"]),
            iam.PolicyStatement(actions=["s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:ListBucket",
                                         "s3:ListBucketMultipartUploads", "s3:PutObject"],
                                resources=[ops_bucket.bucket_arn, ops_bucket.arn_for_objects("firehose-errors/*")]),
            iam.PolicyStatement(actions=["logs:PutLogEvents"], resources=[log_group.log_group_arn]),
        ])
        # Firehose validates the role against Glue on create; give IAM time to propagate first.
        wait_fn = common.python_function(self, "IamWaitFunction", function_name=f"{id}-iam-wait",
            handler="iam_wait.handler", asset="custom_resources", memory_mb=128, timeout_s=120)
        iam_wait = CustomResource(self, "FirehoseRoleIamWait", service_token=wait_fn.function_arn,
            properties={"RoleArn": role.role_arn, "Seconds": "30"})
        iam_wait.node.add_dependency(policy)

        stream = firehose.CfnDeliveryStream(self, "UsageEventsFirehose",
            delivery_stream_name=f"{id}-usage-events",
            delivery_stream_type="DirectPut",
            iceberg_destination_configuration=firehose.CfnDeliveryStream.IcebergDestinationConfigurationProperty(
                role_arn=role.role_arn,
                # Must be the bucket-level sub-catalog; the 2-level path makes Firehose miss the table.
                catalog_configuration=firehose.CfnDeliveryStream.CatalogConfigurationProperty(
                    catalog_arn=f"arn:aws:glue:{self.region}:{self.account}:catalog/s3tablescatalog/{catalog['bucket_name']}"),
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(interval_in_seconds=60, size_in_m_bs=1),
                retry_options=firehose.CfnDeliveryStream.RetryOptionsProperty(duration_in_seconds=300),
                cloud_watch_logging_options=firehose.CfnDeliveryStream.CloudWatchLoggingOptionsProperty(
                    enabled=True, log_group_name=log_group.log_group_name, log_stream_name=log_stream.log_stream_name),
                destination_table_configuration_list=[firehose.CfnDeliveryStream.DestinationTableConfigurationProperty(
                    destination_database_name=ICEBERG_NAMESPACE, destination_table_name=ICEBERG_TABLE,
                    unique_keys=["request_id"])],
                s3_configuration=firehose.CfnDeliveryStream.S3DestinationConfigurationProperty(
                    bucket_arn=ops_bucket.bucket_arn, prefix="firehose-errors/", role_arn=role.role_arn),
                s3_backup_mode="FailedDataOnly",
                append_only=False,
            ),
        )
        stream.node.add_dependency(iam_wait)
        stream.add_dependency(catalog["glue_catalog"])
        stream.add_dependency(catalog["table"])
        return stream

    def _grant_athena_read(self, fn, workgroup: athena.CfnWorkGroup, ops_bucket: s3.Bucket, catalog: dict):
        """Athena SELECT on the Iceberg table, results written to the ops bucket."""
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults",
                     "athena:StopQueryExecution"],
            resources=[f"arn:aws:athena:{self.region}:{self.account}:workgroup/{workgroup.name}"]))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetBucketLocation", "s3:ListBucket"], resources=[ops_bucket.bucket_arn]))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
            resources=[ops_bucket.arn_for_objects("athena-results/*")]))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:GetCatalog", "glue:GetDatabase", "glue:GetTable", "glue:GetDatabases", "glue:GetTables"],
            resources=[f"arn:aws:glue:{self.region}:{self.account}:{r}" for r in (
                "catalog", "catalog/s3tablescatalog", "catalog/s3tablescatalog/*", "database/*", "table/*/*")]))
        fn.add_to_role_policy(iam.PolicyStatement(actions=["lakeformation:GetDataAccess"], resources=["*"]))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:GetTable",
                     "s3tables:GetTableData", "s3tables:GetTableMetadataLocation"],
            resources=[catalog["bucket_arn"], f"{catalog['bucket_arn']}/table/*"]))
