# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK Hub Stack for Bedrock Invocation Analytics."""

from aws_cdk import (
    Stack,
    CfnCondition,
    CfnParameter,
    CfnOutput,
    CustomResource,
    Duration,
    Fn,
    RemovalPolicy,
    aws_athena as athena,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_glue as glue,
    aws_iam as iam,
    aws_kinesisfirehose as firehose,
    aws_lakeformation as lakeformation,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3tables as s3tables,
    custom_resources as cr,
)
from constructs import Construct

BEDROCK_LOGGING_CR_CODE = """
import boto3, json, urllib3
http = urllib3.PoolManager()
def send(event, ctx, status, data={}):
    try:
        body = json.dumps({'Status': status, 'Reason': str(data.get('Error','')),
            'PhysicalResourceId': ctx.log_stream_name, 'StackId': event['StackId'],
            'RequestId': event['RequestId'], 'LogicalResourceId': event['LogicalResourceId'], 'Data': data})
        resp = http.request('PUT', event['ResponseURL'], headers={'content-type':'','content-length':str(len(body))}, body=body)
        print(f'cfn response status: {resp.status}')
    except Exception as e:
        print(f'Failed to send cfn response: {e}')
def handler(event, context):
    try:
        client = boto3.client('bedrock')
        rt = event['RequestType']
        props = event['ResourceProperties']
        if rt in ('Create', 'Update'):
            client.put_model_invocation_logging_configuration(loggingConfig={
                's3Config': {'bucketName': props['BucketName'], 'keyPrefix': props['KeyPrefix']},
                'textDataDeliveryEnabled': True, 'imageDataDeliveryEnabled': True, 'embeddingDataDeliveryEnabled': True,
            })
        elif rt == 'Delete':
            try: client.delete_model_invocation_logging_configuration()
            except: pass
        send(event, context, 'SUCCESS')
    except Exception as e:
        print(e)
        send(event, context, 'FAILED', {'Error': str(e)})
"""


class HubStack(Stack):
    # DEDUP marker TTL in the usage-stats table. Fixed: long enough to cover any
    # retry window we'd reasonably allow, short enough to auto-clean.
    COST_AGG_DEDUP_TTL_HOURS = 24

    def __init__(self, scope: Construct, id: str,
                 cost_agg_interval_min: int = 5,
                 spoke_accounts: list[str] | None = None,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── Parameters ──
        existing_bucket_name = CfnParameter(
            self, "ExistingBucketName",
            type="String",
            default="",
            description="Leave empty to create a new bucket, or specify an existing bucket name.",
        )
        log_prefix = CfnParameter(
            self, "LogPrefix",
            type="String",
            default="bedrock/invocation-logs/",
            description="S3 key prefix for invocation logs.",
        )
        log_retention_days = CfnParameter(
            self, "LogRetentionDays",
            type="Number",
            default=365,
            description="Days before logs expire (only for newly created bucket).",
        )

        # ── Condition: create new bucket or use existing ──
        create_new = CfnCondition(self, "CreateNewBucket",
            expression=Fn.condition_equals(existing_bucket_name.value_as_string, ""),
        )

        # ── S3 Bucket (conditional) ──
        new_bucket = s3.CfnBucket(
            self, "LogsBucket",
            bucket_name=f"bedrock-logs-{self.account}-{self.region}",
            bucket_encryption=s3.CfnBucket.BucketEncryptionProperty(
                server_side_encryption_configuration=[
                    s3.CfnBucket.ServerSideEncryptionRuleProperty(
                        server_side_encryption_by_default=s3.CfnBucket.ServerSideEncryptionByDefaultProperty(
                            sse_algorithm="AES256",
                        ),
                    ),
                ],
            ),
            public_access_block_configuration=s3.CfnBucket.PublicAccessBlockConfigurationProperty(
                block_public_acls=True, block_public_policy=True,
                ignore_public_acls=True, restrict_public_buckets=True,
            ),
            notification_configuration=s3.CfnBucket.NotificationConfigurationProperty(
                event_bridge_configuration=s3.CfnBucket.EventBridgeConfigurationProperty(event_bridge_enabled=True),
            ),
            lifecycle_configuration=s3.CfnBucket.LifecycleConfigurationProperty(
                rules=[s3.CfnBucket.RuleProperty(
                    id="TransitionAndExpire", status="Enabled",
                    prefix=log_prefix.value_as_string,
                    transitions=[s3.CfnBucket.TransitionProperty(
                        storage_class="STANDARD_IA", transition_in_days=90,
                    )],
                    expiration_in_days=log_retention_days.value_as_number,
                )],
            ),
        )
        new_bucket.cfn_options.condition = create_new
        new_bucket.apply_removal_policy(RemovalPolicy.RETAIN)

        # Bucket policy for Bedrock logging
        bucket_policy = s3.CfnBucketPolicy(
            self, "LogsBucketPolicy",
            bucket=new_bucket.ref,
            policy_document={
                "Version": "2012-10-17",
                "Statement": [{
                    "Sid": "AllowBedrockLogging",
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock.amazonaws.com"},
                    "Action": "s3:PutObject",
                    "Resource": Fn.join("", ["arn:aws:s3:::", new_bucket.ref, "/", log_prefix.value_as_string, "*"]),
                    "Condition": {"StringEquals": {"aws:SourceAccount": self.account}},
                }],
            },
        )
        bucket_policy.cfn_options.condition = create_new

        # Resolve bucket name
        bucket_name_resolved = Fn.condition_if(
            create_new.logical_id, new_bucket.ref, existing_bucket_name.value_as_string,
        ).to_string()

        bucket = s3.Bucket.from_bucket_name(self, "ResolvedBucket", bucket_name_resolved)

        # ── Bedrock Logging Custom Resource ──
        logging_role = iam.Role(self, "BedrockLoggingRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
            inline_policies={"bedrock": iam.PolicyDocument(statements=[
                iam.PolicyStatement(actions=[
                    "bedrock:PutModelInvocationLoggingConfiguration",
                    "bedrock:GetModelInvocationLoggingConfiguration",
                    "bedrock:DeleteModelInvocationLoggingConfiguration",
                ], resources=["*"]),
            ])},
        )
        logging_fn = _lambda.Function(self, "BedrockLoggingFunction",
            function_name=f"{id}-bedrock-invocation-setup",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler", timeout=Duration.seconds(30),
            role=logging_role,
            code=_lambda.Code.from_inline(BEDROCK_LOGGING_CR_CODE),
        )
        CustomResource(self, "BedrockLogging",
            service_token=logging_fn.function_arn,
            properties={"BucketName": bucket_name_resolved, "KeyPrefix": log_prefix.value_as_string},
        )

        # ── DynamoDB Tables ──
        usage_stats_table = dynamodb.Table(self, "UsageStatsTable",
            table_name=f"{id}-usage-stats",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )
        model_pricing_table = dynamodb.Table(self, "ModelPricingTable",
            table_name=f"{id}-model-pricing",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── Lake Formation settings: register CDK exec role as LF admin ──
        # Using the CDK CloudFormation execution role (bootstrap role) — it's only active during deployments, minimizing blast radius.
        lf_admin_role = iam.Role(self, "LFGrantsRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole")],
            inline_policies={"lf-grants": iam.PolicyDocument(statements=[
                iam.PolicyStatement(
                    actions=["lakeformation:GrantPermissions", "lakeformation:RevokePermissions",
                             "lakeformation:GetDataAccess"],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=["glue:GetTable", "glue:GetDatabase", "glue:GetCatalog"],
                    resources=[f"arn:aws:glue:{self.region}:{self.account}:*"],
                ),
                iam.PolicyStatement(
                    actions=["s3tables:GetTableBucket", "s3tables:GetTable",
                             "s3tables:GetNamespace", "s3tables:GetTableMetadataLocation"],
                    resources=[
                        f"arn:aws:s3tables:{self.region}:{self.account}:bucket/*",
                        f"arn:aws:s3tables:{self.region}:{self.account}:bucket/*/table/*",
                    ],
                ),
            ])},
        )
        cdk_exec_role_arn = (
            f"arn:aws:iam::{self.account}:role/"
            f"cdk-hnb659fds-cfn-exec-role-{self.account}-{self.region}"
        )
        lf_settings = lakeformation.CfnDataLakeSettings(self, "LFSettings",
            admins=[
                # CDK CloudFormation exec role — for general LF ops at deploy time
                lakeformation.CfnDataLakeSettings.DataLakePrincipalProperty(
                    data_lake_principal_identifier=cdk_exec_role_arn),
                # Dedicated role for AwsCustomResource below to call GrantPermissions
                lakeformation.CfnDataLakeSettings.DataLakePrincipalProperty(
                    data_lake_principal_identifier=lf_admin_role.role_arn),
            ],
        )

        # ── S3 Tables (Iceberg) for L1 usage events ──
        table_bucket_name = f"bedrock-analytics-{self.account}"
        table_bucket_arn = f"arn:aws:s3tables:{self.region}:{self.account}:bucket/{table_bucket_name}"

        table_bucket = s3tables.CfnTableBucket(self, "UsageTableBucket",
            table_bucket_name=table_bucket_name,
        )
        table_bucket.apply_removal_policy(RemovalPolicy.RETAIN)

        iceberg_namespace = s3tables.CfnNamespace(self, "IcebergNamespace",
            namespace="bedrock_analytics",
            table_bucket_arn=table_bucket_arn,
        )
        iceberg_namespace.add_dependency(table_bucket)
        iceberg_namespace.apply_removal_policy(RemovalPolicy.RETAIN)

        _schema_fields = [
            (1,  "account_id",              "string",      True),
            (2,  "region",                  "string",      True),
            (3,  "request_id",              "string",      True),
            (4,  "ts",                      "timestamptz", True),
            (5,  "operation",               "string",      False),
            (6,  "model_id",                "string",      False),
            (7,  "caller",                  "string",      False),
            (8,  "input_tokens",            "long",        False),
            (9,  "output_tokens",           "long",        False),
            (10, "cache_read_tokens",       "long",        False),
            (11, "cache_write_5m_tokens",   "long",        False),
            (12, "cache_write_1h_tokens",   "long",        False),
            (13, "cache_write_total_tokens","long",        False),
            (14, "latency_ms",              "int",         False),
            (15, "time_to_first_token_ms",  "int",         False),
            (16, "error_code",              "string",      False),
            (17, "source_s3_key",           "string",      False),
            (18, "parsed_at",              "timestamptz", False),
            (19, "schema_version",          "int",         False),
        ]
        usage_events_table = s3tables.CfnTable(self, "UsageEventsTable",
            table_bucket_arn=table_bucket_arn,
            namespace="bedrock_analytics",
            table_name="usage_events",
            open_table_format="ICEBERG",
            iceberg_metadata=s3tables.CfnTable.IcebergMetadataProperty(
                iceberg_schema=s3tables.CfnTable.IcebergSchemaProperty(
                    schema_field_list=[
                        s3tables.CfnTable.SchemaFieldProperty(name=n, type=t, required=r)
                        for (_, n, t, r) in _schema_fields
                    ],
                ),
                iceberg_partition_spec=s3tables.CfnTable.IcebergPartitionSpecProperty(
                    fields=[
                        s3tables.CfnTable.IcebergPartitionFieldProperty(source_id=1, transform="identity", name="account_id"),
                        s3tables.CfnTable.IcebergPartitionFieldProperty(source_id=4, transform="year", name="year"),
                        s3tables.CfnTable.IcebergPartitionFieldProperty(source_id=4, transform="month", name="month"),
                        s3tables.CfnTable.IcebergPartitionFieldProperty(source_id=4, transform="day", name="day"),
                    ],
                ),
            ),
        )
        usage_events_table.add_dependency(iceberg_namespace)
        usage_events_table.apply_removal_policy(RemovalPolicy.RETAIN)

        # ── Glue federated catalog for S3 Tables ──
        iam_allowed_principal = glue.CfnCatalog.PrincipalPermissionsProperty(
            principal=glue.CfnCatalog.DataLakePrincipalProperty(
                data_lake_principal_identifier="IAM_ALLOWED_PRINCIPALS"),
            permissions=["ALL"],
        )
        glue_s3tables_catalog = glue.CfnCatalog(self, "GlueS3TablesCatalog",
            name="s3tablescatalog",
            federated_catalog=glue.CfnCatalog.FederatedCatalogProperty(
                identifier=f"arn:aws:s3tables:{self.region}:{self.account}:bucket/*",
                connection_name="aws:s3tables",
            ),
            create_database_default_permissions=[iam_allowed_principal],
            create_table_default_permissions=[iam_allowed_principal],
            allow_full_table_external_data_access="True",
        )
        glue_s3tables_catalog.add_dependency(table_bucket)
        glue_s3tables_catalog.apply_removal_policy(RemovalPolicy.RETAIN)

        self.table_bucket_arn = table_bucket_arn
        self.iceberg_table_name = "usage_events"
        self.iceberg_namespace = "bedrock_analytics"

        # ── Firehose delivery stream → S3 Tables Iceberg ──
        # Lambda parse_log puts records here; Firehose buffers (60s / 1MB) then writes to
        # usage_events with upsert on request_id. S3 error/backup prefix reuses the main
        # log bucket to avoid a second bucket.
        firehose_log_group = logs.LogGroup(self, "FirehoseLogGroup",
            log_group_name=f"/aws/kinesisfirehose/{id}-usage-events",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        firehose_log_stream = logs.LogStream(self, "FirehoseLogStream",
            log_group=firehose_log_group,
            log_stream_name="IcebergDelivery",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Firehose catalog ARN must point to the bucket-level federated sub-catalog.
        # The 2-level path (catalog/s3tablescatalog) makes Firehose look at the
        # wrong layer and it can't find the table. CFN validator regex
        # catalog(?:(/[a-z0-9_-]+){1,2})? allows 1 or 2 segments → 3-level is OK.
        s3tables_catalog_arn = (
            f"arn:aws:glue:{self.region}:{self.account}:catalog/s3tablescatalog/"
            f"bedrock-analytics-{self.account}"
        )

        # Firehose role — pre-created by deploy.sh to avoid IAM propagation delay.
        # CDK only references it; deploy.sh manages the role and its policy.
        firehose_role = iam.Role.from_role_name(self, "FirehoseDeliveryRole",
            role_name=f"{id}-FirehoseDeliveryRole",
        )

        # ── LF Grant: give Firehose role access to federated table ──
        # Without explicit LF grant, Firehose's glue:GetTable preflight check fails.
        # IAM_ALLOWED_PRINCIPALS doesn't auto-apply to tables federated from S3 Tables.
        # ── LF Grant: done in deploy.sh pre-deploy step ──
        # LF grant must be applied by an existing LF admin (wsadmin) before CDK deploy,
        # because a newly-created LF admin role's permissions don't propagate in time.

        usage_events_stream = firehose.CfnDeliveryStream(self, "UsageEventsFirehose",
            delivery_stream_name=f"{id}-usage-events",
            delivery_stream_type="DirectPut",
            iceberg_destination_configuration=firehose.CfnDeliveryStream.IcebergDestinationConfigurationProperty(
                role_arn=firehose_role.role_arn,
                catalog_configuration=firehose.CfnDeliveryStream.CatalogConfigurationProperty(
                    catalog_arn=s3tables_catalog_arn,
                ),
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60, size_in_m_bs=1,
                ),
                cloud_watch_logging_options=firehose.CfnDeliveryStream.CloudWatchLoggingOptionsProperty(
                    enabled=True,
                    log_group_name=firehose_log_group.log_group_name,
                    log_stream_name=firehose_log_stream.log_stream_name,
                ),
                destination_table_configuration_list=[
                    firehose.CfnDeliveryStream.DestinationTableConfigurationProperty(
                        destination_database_name="bedrock_analytics",
                        destination_table_name="usage_events",
                        unique_keys=["request_id"],
                    ),
                ],
                s3_configuration=firehose.CfnDeliveryStream.S3DestinationConfigurationProperty(
                    bucket_arn=f"arn:aws:s3:::{bucket_name_resolved}",
                    prefix="firehose-errors/",
                    role_arn=firehose_role.role_arn,
                ),
                s3_backup_mode="FailedDataOnly",
                append_only=False,  # enable upsert using unique_keys
            ),
        )
        usage_events_stream.add_dependency(lf_settings)
        usage_events_stream.add_dependency(glue_s3tables_catalog)
        usage_events_stream.add_dependency(usage_events_table)

        self.usage_events_firehose_name = usage_events_stream.delivery_stream_name
        self.usage_events_firehose_arn = usage_events_stream.attr_arn

        # ── Lambda: Process each new log file ──
        process_log_fn = _lambda.Function(self, "ProcessLogFunction",
            function_name=f"{id}-process-log",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="process_log.handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(60), memory_size=256,
            environment={
                "USAGE_STATS_TABLE": usage_stats_table.table_name,
                "MODEL_PRICING_TABLE": model_pricing_table.table_name,
            },
        )
        bucket.grant_read(process_log_fn)
        usage_stats_table.grant_read_write_data(process_log_fn)
        model_pricing_table.grant_read_data(process_log_fn)

        # ── Lambda: V3 L1 parse → Firehose (deployed but not yet wired to S3 trigger) ──
        parse_log_fn = _lambda.Function(self, "ParseLogFunction",
            function_name=f"{id}-parse-log",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="parse_log.handler",
            code=_lambda.Code.from_asset("lambda"),
            # Peak ~112MB after caching the Firehose client; 256MB gives 2x headroom.
            timeout=Duration.seconds(60), memory_size=256,
            environment={
                "FIREHOSE_STREAM": usage_events_stream.delivery_stream_name or "",
                "HUB_REGION": self.region,
                "SCHEMA_VERSION": "1",
            },
        )
        bucket.grant_read(parse_log_fn)
        parse_log_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
            resources=[usage_events_stream.attr_arn],
        ))

        # ── Athena workgroup + results bucket (for L2 SELECT + ad-hoc queries) ──
        # Reuse the main log bucket with an athena-results/ prefix.
        athena_workgroup = athena.CfnWorkGroup(self, "AthenaWorkgroup",
            name=f"{id}-compute",
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{bucket_name_resolved}/athena-results/",
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=False,
            ),
        )

        # ── Lambda: V3 L2 compute_cost (Iceberg → DDB aggregates) ──
        iceberg_catalog = f"s3tablescatalog/bedrock-analytics-{self.account}"
        compute_cost_fn = _lambda.Function(self, "ComputeCostFunction",
            function_name=f"{id}-compute-cost",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="compute_cost.handler",
            code=_lambda.Code.from_asset("lambda"),
            # Athena query poll + paginate results + TransactWriteItems per event.
            # Observed peak 96MB; 256MB is plenty.
            timeout=Duration.seconds(300), memory_size=256,
            environment={
                "USAGE_STATS_TABLE": usage_stats_table.table_name,
                "MODEL_PRICING_TABLE": model_pricing_table.table_name,
                "ATHENA_WORKGROUP": athena_workgroup.name or "",
                "ATHENA_OUTPUT_S3": f"s3://{bucket_name_resolved}/athena-results/",
                "ICEBERG_CATALOG": iceberg_catalog,
                "ICEBERG_DATABASE": "bedrock_analytics",
                "ICEBERG_TABLE": "usage_events",
                "WINDOW_MINUTES": str(cost_agg_interval_min),
                "DEDUP_TTL_HOURS": str(self.COST_AGG_DEDUP_TTL_HOURS),
            },
        )
        usage_stats_table.grant_read_write_data(compute_cost_fn)
        model_pricing_table.grant_read_data(compute_cost_fn)

        # Athena query permissions + result S3 prefix
        compute_cost_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "athena:StartQueryExecution", "athena:GetQueryExecution",
                "athena:GetQueryResults", "athena:StopQueryExecution",
            ],
            resources=[
                f"arn:aws:athena:{self.region}:{self.account}:workgroup/{athena_workgroup.name}",
            ],
        ))
        compute_cost_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket",
                     "s3:PutObject", "s3:AbortMultipartUpload",
                     "s3:ListMultipartUploadParts"],
            resources=[
                f"arn:aws:s3:::{bucket_name_resolved}",
                f"arn:aws:s3:::{bucket_name_resolved}/athena-results/*",
            ],
        ))
        # Glue federated catalog read (same ARN format as Firehose role — see comments above)
        compute_cost_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["glue:GetCatalog", "glue:GetDatabase", "glue:GetTable",
                     "glue:GetDatabases", "glue:GetTables"],
            resources=[
                f"arn:aws:glue:{self.region}:{self.account}:catalog",
                f"arn:aws:glue:{self.region}:{self.account}:catalog/s3tablescatalog",
                f"arn:aws:glue:{self.region}:{self.account}:catalog/s3tablescatalog/*",
                f"arn:aws:glue:{self.region}:{self.account}:database/*",
                f"arn:aws:glue:{self.region}:{self.account}:table/*/*",
            ],
        ))
        # S3 Tables uses Lake Formation credential vending under the hood
        compute_cost_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["lakeformation:GetDataAccess"],
            resources=["*"],
        ))
        # S3 Tables API: Athena needs this to read Iceberg data files.
        # Same pattern as Firehose delivery role — the exact action list required
        # is not well documented, so use s3tables:* scoped to this account.
        compute_cost_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3tables:*"],
            resources=[
                f"arn:aws:s3tables:{self.region}:{self.account}:bucket/*",
                f"arn:aws:s3tables:{self.region}:{self.account}:bucket/*/table/*",
            ],
        ))

        # L2 schedule (EventBridge), deployed but DISABLED until Step 5 cutover
        l2_schedule = events.Rule(self, "ComputeCostSchedule",
            rule_name=f"{id}-compute-cost-schedule",
            schedule=events.Schedule.rate(Duration.minutes(cost_agg_interval_min)),
            enabled=False,  # enable in Step 5 cutover
            targets=[targets.LambdaFunction(compute_cost_fn)],
        )

        # ── EventBridge: S3 Object Created → Process Log ──
        events.Rule(self, "NewLogFileTrigger",
            rule_name=f"{id}-new-log-trigger",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [bucket_name_resolved]},
                    "object": {"key": [{"suffix": ".json.gz"}]},
                },
            ),
            targets=[targets.LambdaFunction(process_log_fn)],
        )

        # V3 L1 trigger (parse_log → Firehose).  Deployed DISABLED.
        # Step 5 cutover: disable NewLogFileTrigger above, enable this one.
        events.Rule(self, "NewLogFileTriggerV3",
            rule_name=f"{id}-new-log-trigger-v3",
            enabled=False,
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [bucket_name_resolved]},
                    "object": {"key": [{"suffix": ".json.gz"}]},
                },
            ),
            targets=[targets.LambdaFunction(parse_log_fn)],
        )

        # ── Lambda: Aggregate hourly→daily→monthly ──
        aggregate_stats_fn = _lambda.Function(self, "AggregateStatsFunction",
            function_name=f"{id}-aggregate-stats",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="aggregate_stats.handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(300), memory_size=256,
            environment={"USAGE_STATS_TABLE": usage_stats_table.table_name},
        )
        usage_stats_table.grant_read_write_data(aggregate_stats_fn)

        events.Rule(self, "DailyAggregateSchedule",
            rule_name=f"{id}-daily-aggregate",
            schedule=events.Schedule.cron(minute="15", hour="0"),
            targets=[targets.LambdaFunction(aggregate_stats_fn, event=events.RuleTargetInput.from_object({"type": "daily"}))],
        )
        events.Rule(self, "MonthlyAggregateSchedule",
            rule_name=f"{id}-monthly-aggregate",
            schedule=events.Schedule.cron(minute="0", hour="1", day="1"),
            targets=[targets.LambdaFunction(aggregate_stats_fn, event=events.RuleTargetInput.from_object({"type": "monthly"}))],
        )

        # ── Lambda: Sync pricing from LiteLLM (weekly) ──
        sync_pricing_fn = _lambda.Function(self, "SyncPricingFunction",
            function_name=f"{id}-sync-pricing",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="sync_pricing.handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(120), memory_size=256,
            environment={
                "MODEL_PRICING_TABLE": model_pricing_table.table_name,
                "USAGE_STATS_TABLE": usage_stats_table.table_name,
            },
        )
        model_pricing_table.grant_read_write_data(sync_pricing_fn)
        usage_stats_table.grant_write_data(sync_pricing_fn)

        events.Rule(self, "WeeklyPricingSyncSchedule",
            rule_name=f"{id}-weekly-pricing-sync",
            schedule=events.Schedule.cron(minute="0", hour="21", week_day="SUN"),
            targets=[targets.LambdaFunction(sync_pricing_fn)],
        )

        # ── Class name update ──
        self.usage_stats_table = usage_stats_table
        self.model_pricing_table = model_pricing_table

        # ── IAM Role for Spoke Lambdas (cross-account) ──
        # Source of truth: app.py reads config.yaml and passes spoke_accounts directly.
        # Context override stays as a fallback for ad-hoc CLI use.
        if not spoke_accounts:
            ctx_str = self.node.try_get_context("spoke_accounts") or ""
            spoke_accounts = [a for a in ctx_str.split(",") if a]
        if spoke_accounts:
            spoke_write_role = iam.Role(self, "SpokeWriteRole",
                role_name="BedrockAnalytics-SpokeWriteRole",
                assumed_by=iam.CompositePrincipal(*[
                    iam.ArnPrincipal(f"arn:aws:iam::{acct}:root")
                    for acct in spoke_accounts
                ]),
            )
            # Usage stats: read + write
            usage_stats_table.grant_read_write_data(spoke_write_role)
            # Pricing: read only
            model_pricing_table.grant_read_data(spoke_write_role)
            # Firehose: put records (V3 L1 pipeline)
            spoke_write_role.add_to_policy(iam.PolicyStatement(
                actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
                resources=[usage_events_stream.attr_arn],
            ))

            CfnOutput(self, "SpokeWriteRoleArn", value=spoke_write_role.role_arn)

        # ── WebUI deployment (TODO: evaluate lightweight hosting options) ──

        # ── Outputs ──
        CfnOutput(self, "BucketName", value=bucket_name_resolved)
        CfnOutput(self, "UsageStatsTableName", value=usage_stats_table.table_name)
        CfnOutput(self, "ModelPricingTableName", value=model_pricing_table.table_name)
        CfnOutput(self, "ProcessLogFunctionName", value=process_log_fn.function_name)
        CfnOutput(self, "AggregateStatsFunctionName", value=aggregate_stats_fn.function_name)
        CfnOutput(self, "UsageTableBucketArn", value=table_bucket_arn)
        CfnOutput(self, "UsageEventsTableArn", value=usage_events_table.attr_table_arn)
        CfnOutput(self, "UsageEventsFirehoseName", value=usage_events_stream.delivery_stream_name or "")
        CfnOutput(self, "UsageEventsFirehoseArn", value=usage_events_stream.attr_arn)
        CfnOutput(self, "ParseLogFunctionName", value=parse_log_fn.function_name)
        CfnOutput(self, "ComputeCostFunctionName", value=compute_cost_fn.function_name)
        CfnOutput(self, "AthenaWorkgroupName", value=athena_workgroup.name or "")
