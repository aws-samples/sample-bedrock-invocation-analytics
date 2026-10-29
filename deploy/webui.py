# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""WebUI: SPA + API Lambda behind an internal ALB, optional CloudFront (VPC origin), Cognito sign-in."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as targets,
    aws_iam as iam,
    custom_resources as cr,
)
from constructs import Construct

import common

LAMBDA_PACKAGE = Path(__file__).resolve().parent.parent / "build" / "webui-lambda"
DEFAULT_ADMIN_GROUP = "bedrock-analytics-admin"
ADMIN_GROUP_DESCRIPTION = "Bedrock Invocation Analytics: edit pricing"
# Tokens from USER_PASSWORD_AUTH may call UpdateUserAttributes, and Cognito treats an unset or empty WriteAttributes
# as "all mutable attributes" (custom ones included). The WebUI writes none; Cognito requires required attributes
# (email) to stay writable, so that is the narrowest list it accepts.
CLIENT_WRITE_ATTRIBUTES = ["email"]


@dataclass
class WebUiConfig:
    """`webui` section of config.yaml, plus values resolved by app.py (subnet AZs, CloudFront prefix list)."""
    cloudfront: bool = False
    vpc_id: str = ""
    subnets: list[dict] = field(default_factory=list)  # [{"subnet_id", "availability_zone"}] when vpc_id is set
    allowed_cidrs: list[str] = field(default_factory=list)
    certificate_arn: str = ""
    cognito_user_pool_id: str = ""
    admin_group: str = ""  # existing group to reuse; empty = the stack creates DEFAULT_ADMIN_GROUP
    initial_admin_email: str = ""
    cloudfront_prefix_list_id: str = ""
    account_names: dict[str, str] = field(default_factory=dict)


class WebUi(Construct):
    def __init__(self, scope: Construct, id: str, *, config: WebUiConfig,
                 usage_table: dynamodb.ITable, pricing_table: dynamodb.ITable, data_sources: dict[str, str]):
        super().__init__(scope, id)
        stack = Stack.of(self)
        if not LAMBDA_PACKAGE.is_dir():
            raise FileNotFoundError(f"{LAMBDA_PACKAGE} is missing; run scripts/build_webui.sh first")

        pool_id, client_id, pool_arn = self._cognito(config)
        fn = common.python_function(
            self, "Function", function_name=f"{stack.stack_name}-webui", handler="app.handler",
            asset=str(LAMBDA_PACKAGE), timeout_s=29, memory_mb=512,
            environment={
                "USER_POOL_ID": pool_id,
                "USER_POOL_CLIENT_ID": client_id,
                "ADMIN_GROUP": config.admin_group or DEFAULT_ADMIN_GROUP,
                "USAGE_STATS_TABLE": usage_table.table_name,
                "MODEL_PRICING_TABLE": pricing_table.table_name,
                "ACCOUNT_NAMES": json.dumps(config.account_names),
                # Shown in the SPA info panel: where the dashboard reads from and where to run ad-hoc queries.
                "DATA_SOURCES": stack.to_json_string({"region": stack.region, "usage_table": usage_table.table_name,
                                                      "pricing_table": pricing_table.table_name, **data_sources}),
                "POWERTOOLS_SERVICE_NAME": "webui",
            },
        )
        usage_table.grant_read_data(fn)
        pricing_table.grant_read_write_data(fn)
        fn.add_to_role_policy(iam.PolicyStatement(actions=["cloudwatch:GetMetricData"], resources=["*"]))
        fn.add_to_role_policy(iam.PolicyStatement(actions=["cognito-idp:AdminGetUser"], resources=[pool_arn]))

        vpc, subnets = self._network(config)
        sg = ec2.SecurityGroup(self, "AlbSecurityGroup", vpc=vpc, allow_all_outbound=False,
                               description="WebUI internal ALB")
        alb = elbv2.ApplicationLoadBalancer(self, "Alb", vpc=vpc, internet_facing=False, security_group=sg,
                                            vpc_subnets=subnets, drop_invalid_header_fields=True,
                                            idle_timeout=Duration.seconds(60))
        target = targets.LambdaTarget(fn)

        # 443 for intranet users when a certificate is given; 80 for CloudFront (VPC origin) and, without a certificate, for intranet users.
        https = bool(config.certificate_arn)
        if https:
            cert = acm.Certificate.from_certificate_arn(self, "Certificate", config.certificate_arn)
            alb.add_listener("Https", port=443, protocol=elbv2.ApplicationProtocol.HTTPS, certificates=[cert],
                             ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS, open=False).add_targets("Lambda", targets=[target])
            for cidr in config.allowed_cidrs:
                sg.add_ingress_rule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(443), "Intranet access")
        if config.cloudfront or not https:
            alb.add_listener("Http", port=80, protocol=elbv2.ApplicationProtocol.HTTP, open=False).add_targets("Lambda", targets=[target])
            if not https:
                for cidr in config.allowed_cidrs:
                    sg.add_ingress_rule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(80), "Intranet access (plaintext HTTP)")
            if config.cloudfront:
                sg.add_ingress_rule(ec2.Peer.prefix_list(config.cloudfront_prefix_list_id), ec2.Port.tcp(80), "CloudFront VPC origin")

        url = f"{'https' if https else 'http'}://{alb.load_balancer_dns_name}"
        if config.cloudfront:
            distribution = self._cloudfront(alb)
            url = f"https://{distribution.distribution_domain_name}"
            CfnOutput(stack, "WebUiCloudFrontDomain", value=distribution.distribution_domain_name)
        CfnOutput(stack, "WebUiUrl", value=url)
        CfnOutput(stack, "WebUiAlbDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(stack, "WebUiUserPoolId", value=pool_id)
        if not https and config.allowed_cidrs:
            CfnOutput(stack, "WebUiPlaintextWarning", value="Direct ALB access is plaintext HTTP; set webui.certificate_arn for HTTPS")

    def _cognito(self, config: WebUiConfig) -> tuple[str, str, str]:
        """(pool id, app client id, pool ARN). An existing pool may be in another region: its ID carries the region."""
        if config.cognito_user_pool_id:
            return self._existing_pool_client(config.cognito_user_pool_id, create_admin_group=not config.admin_group)
        pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"{Stack.of(self).stack_name}-webui",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),  # email is the username
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(email=cognito.StandardAttribute(required=True, mutable=True)),
            password_policy=cognito.PasswordPolicy(min_length=8, require_lowercase=True, require_uppercase=True,
                                                   require_digits=True, require_symbols=False,
                                                   temp_password_validity=Duration.days(3)),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        client = pool.add_client(
            "WebUiClient",
            user_pool_client_name=f"{Stack.of(self).stack_name}-webui",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            write_attributes=cognito.ClientAttributes().with_standard_attributes(email=True),
            disable_o_auth=True,
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.hours(12),
            enable_token_revocation=True,
        )
        # A created pool is new, so its admin group is always the stack's to create (named admin_group if set).
        admin_group = config.admin_group or DEFAULT_ADMIN_GROUP
        group = cognito.CfnUserPoolGroup(self, "GroupAdmin", user_pool_id=pool.user_pool_id, group_name=admin_group,
                                         description=ADMIN_GROUP_DESCRIPTION)
        if config.initial_admin_email:
            user = cognito.CfnUserPoolUser(self, "InitialAdmin", user_pool_id=pool.user_pool_id,
                                           username=config.initial_admin_email,
                                           user_attributes=[cognito.CfnUserPoolUser.AttributeTypeProperty(name="email", value=config.initial_admin_email),
                                                            cognito.CfnUserPoolUser.AttributeTypeProperty(name="email_verified", value="true")],
                                           desired_delivery_mediums=["EMAIL"])
            attach = cognito.CfnUserPoolUserToGroupAttachment(self, "InitialAdminGroup", user_pool_id=pool.user_pool_id,
                                                              group_name=admin_group, username=user.ref)
            attach.add_dependency(group)
        return pool.user_pool_id, client.user_pool_client_id, pool.user_pool_arn

    def _existing_pool_client(self, pool_id: str, create_admin_group: bool) -> tuple[str, str, str]:
        """Add the app client and groups to an existing pool through SDK calls in the pool's region.
        CloudFormation resources can only target the stack's region; the pool's own settings are never changed."""
        stack = Stack.of(self)
        region = pool_id.split("_", 1)[0]
        pool_arn = f"arn:aws:cognito-idp:{region}:{stack.account}:userpool/{pool_id}"
        policy = cr.AwsCustomResourcePolicy.from_sdk_calls(resources=[pool_arn])
        sdk = lambda action, params, physical=None: cr.AwsSdkCall(  # noqa: E731
            service="CognitoIdentityServiceProvider", action=action, region=region, parameters=params,
            physical_resource_id=physical)
        client_settings = {
            "UserPoolId": pool_id,
            "ClientName": f"{stack.stack_name}-webui",
            "ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            "PreventUserExistenceErrors": "ENABLED",
            "EnableTokenRevocation": True,
            "WriteAttributes": CLIENT_WRITE_ATTRIBUTES,
            "AccessTokenValidity": 60, "IdTokenValidity": 60, "RefreshTokenValidity": 720,
            "TokenValidityUnits": {"AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "minutes"},
        }
        client_id = cr.PhysicalResourceId.from_response("UserPoolClient.ClientId")
        client = cr.AwsCustomResource(
            self, "ExistingPoolClient", policy=policy, install_latest_aws_sdk=False,
            on_create=sdk("createUserPoolClient", {**client_settings, "GenerateSecret": False}, client_id),
            on_update=sdk("updateUserPoolClient", {**client_settings, "ClientId": cr.PhysicalResourceIdReference()}, client_id),
            on_delete=sdk("deleteUserPoolClient", {"UserPoolId": pool_id, "ClientId": cr.PhysicalResourceIdReference()}),
        )
        if create_admin_group:
            # Fails if the group already exists, so a stack delete can never remove a group the stack didn't create.
            group = {"UserPoolId": pool_id, "GroupName": DEFAULT_ADMIN_GROUP, "Description": ADMIN_GROUP_DESCRIPTION}
            cr.AwsCustomResource(
                self, "ExistingPoolGroupAdmin", policy=policy, install_latest_aws_sdk=False,
                on_create=sdk("createGroup", group, cr.PhysicalResourceId.of(DEFAULT_ADMIN_GROUP)),
                on_update=sdk("updateGroup", group, cr.PhysicalResourceId.of(DEFAULT_ADMIN_GROUP)),
                on_delete=sdk("deleteGroup", {"UserPoolId": pool_id, "GroupName": DEFAULT_ADMIN_GROUP}),
            )
        return pool_id, client.get_response_field("UserPoolClient.ClientId"), pool_arn

    def _network(self, config: WebUiConfig) -> tuple[ec2.IVpc, ec2.SubnetSelection]:
        if not config.vpc_id:
            vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=0, restrict_default_security_group=True,
                          subnet_configuration=[
                              # Public subnets exist only so the VPC has an internet gateway (CloudFront VPC origin requirement).
                              ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=26, map_public_ip_on_launch=False),
                              ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
                          ])
            return vpc, ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)
        vpc = ec2.Vpc.from_vpc_attributes(self, "Vpc", vpc_id=config.vpc_id,
                                          availability_zones=sorted({s["availability_zone"] for s in config.subnets}))
        subnets = [ec2.Subnet.from_subnet_attributes(self, f"Subnet{i}", subnet_id=s["subnet_id"],
                                                     availability_zone=s["availability_zone"])
                   for i, s in enumerate(config.subnets)]
        return vpc, ec2.SubnetSelection(subnets=subnets)

    def _cloudfront(self, alb: elbv2.ApplicationLoadBalancer) -> cloudfront.Distribution:
        origin = origins.VpcOrigin.with_application_load_balancer(
            alb, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY, http_port=80,
            read_timeout=Duration.seconds(30))
        # API and HTML: no caching, all viewer headers (incl. Authorization) forwarded.
        dynamic = cloudfront.BehaviorOptions(
            origin=origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
        )
        # Fingerprinted assets are immutable and identical for every user.
        assets = cloudfront.BehaviorOptions(
            origin=origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
        )
        return cloudfront.Distribution(
            self, "Distribution",
            comment=f"{Stack.of(self).stack_name} WebUI",
            default_behavior=dynamic,
            additional_behaviors={"/assets/*": assets},
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )
