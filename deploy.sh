#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Deploy Bedrock Invocation Analytics
# Usage:
#   ./deploy.sh hub [cdk-options]         Deploy primary account stack
#   ./deploy.sh spoke [profile]           Deploy spoke stack(s)
#   ./deploy.sh all                       Deploy hub + all spokes
#   ./deploy.sh [cdk-command]             Raw CDK command (diff, synth, destroy, etc.)
#
# Configuration: config.yaml

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.yaml"
ENV_FILE="$SCRIPT_DIR/.env.deploy"

if [[ ! -f "$CONFIG" ]]; then
    echo "Error: config.yaml not found"
    exit 1
fi

# Parse YAML config — one-shot into shell variables
eval "$(python3 -c "
import yaml
with open('$CONFIG') as f:
    c = yaml.safe_load(f)
primary = next(a for a in c['accounts'] if a.get('primary'))
spokes = [a for a in c['accounts'] if not a.get('primary')]
print(f'PRIMARY_PROFILE=\"{primary[\"profile\"]}\"')
print(f'PRIMARY_REGION=\"{primary[\"region\"]}\"')
print(f'PRIMARY_BUCKET=\"{primary.get(\"bucket\",\"\")}\"')
print(f'LOG_PREFIX=\"{c.get(\"data\",{}).get(\"bedrock_log_prefix\",\"bedrock/invocation-logs/\")}\"')
print(f'WEBUI_USER=\"{c.get(\"webui\",{}).get(\"admin_user\",\"\")}\"')
print(f'WEBUI_PASS=\"{c.get(\"webui\",{}).get(\"admin_pass\",\"\")}\"')
# Spokes as profile:region:bucket lines
spoke_lines = '|'.join(f'{a[\"profile\"]}:{a[\"region\"]}:{a.get(\"bucket\",\"\")}' for a in spokes)
print(f'SPOKE_LIST=\"{spoke_lines}\"')
")"

# Get account ID for a profile
get_account_id() {
    aws sts get-caller-identity --profile "$1" --query Account --output text 2>/dev/null
}

run_cdk() {
    local profile="$1"; shift
    local region="$1"; shift
    export AWS_DEFAULT_REGION="$region"
    cd "$SCRIPT_DIR/deploy" && uv run --project .. cdk "$@" --profile "$profile"
}

CMD="${1:-}"
shift 2>/dev/null || true

case "$CMD" in
    hub)
        echo "=== Deploying Hub (${PRIMARY_PROFILE} / ${PRIMARY_REGION}) ==="
        HUB_ACCOUNT=$(get_account_id "$PRIMARY_PROFILE")

        # Auto-bootstrap if needed
        if ! aws cloudformation describe-stacks --profile "$PRIMARY_PROFILE" --region "$PRIMARY_REGION" \
            --stack-name CDKToolkit &>/dev/null; then
            echo "=== Bootstrapping CDK (${PRIMARY_PROFILE} / ${PRIMARY_REGION}) ==="
            run_cdk "$PRIMARY_PROFILE" "$PRIMARY_REGION" bootstrap
        fi

        # Collect spoke account IDs for SpokeWriteRole trust policy
        SPOKE_IDS=""
        IFS='|' read -ra SPOKE_ENTRIES <<< "$SPOKE_LIST"
        for spoke_info in "${SPOKE_ENTRIES[@]}"; do
            IFS=: read -r profile region bucket <<< "$spoke_info"
            ACCT_ID=$(get_account_id "$profile")
            if [[ -n "$ACCT_ID" ]]; then
                SPOKE_IDS="${SPOKE_IDS:+$SPOKE_IDS,}$ACCT_ID"
            fi
        done

        PARAMS="--parameters LogPrefix=${LOG_PREFIX}"
        [[ -n "$PRIMARY_BUCKET" ]] && PARAMS="$PARAMS --parameters ExistingBucketName=${PRIMARY_BUCKET}"

        SPOKE_CTX=""
        [[ -n "$SPOKE_IDS" ]] && SPOKE_CTX="-c spoke_accounts=${SPOKE_IDS}"

        # Pre-flight: ensure Firehose delivery role exists.
        # This role must be pre-created outside CDK because Firehose does a preflight
        # glue:GetTable check at CREATE time using the role. If the role is brand-new
        # (created in the same CFN stack), IAM propagation delay causes the check to fail.
        # By pre-creating the role here, it's fully propagated before CDK deploy starts.
        FIREHOSE_ROLE="BedrockInvocationAnalytics-FirehoseDeliveryRole"
        if ! aws iam get-role --profile "$PRIMARY_PROFILE" --role-name "$FIREHOSE_ROLE" &>/dev/null; then
            echo "=== Creating Firehose delivery role ==="
            aws iam create-role --profile "$PRIMARY_PROFILE" \
                --role-name "$FIREHOSE_ROLE" \
                --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"firehose.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
            aws iam put-role-policy --profile "$PRIMARY_PROFILE" \
                --role-name "$FIREHOSE_ROLE" --policy-name delivery \
                --policy-document "{
              \"Version\":\"2012-10-17\",
              \"Statement\":[
                {\"Sid\":\"GlueCatalog\",\"Effect\":\"Allow\",\"Action\":[\"glue:GetDatabase\",\"glue:GetDatabases\",\"glue:GetTable\",\"glue:GetTables\",\"glue:UpdateTable\"],\"Resource\":[\"arn:aws:glue:${PRIMARY_REGION}:${HUB_ACCOUNT}:catalog\",\"arn:aws:glue:${PRIMARY_REGION}:${HUB_ACCOUNT}:catalog/s3tablescatalog\",\"arn:aws:glue:${PRIMARY_REGION}:${HUB_ACCOUNT}:catalog/s3tablescatalog/*\",\"arn:aws:glue:${PRIMARY_REGION}:${HUB_ACCOUNT}:database/*\",\"arn:aws:glue:${PRIMARY_REGION}:${HUB_ACCOUNT}:table/*/*\"]},
                {\"Sid\":\"S3Tables\",\"Effect\":\"Allow\",\"Action\":\"s3tables:*\",\"Resource\":[\"arn:aws:s3tables:${PRIMARY_REGION}:${HUB_ACCOUNT}:bucket/*\",\"arn:aws:s3tables:${PRIMARY_REGION}:${HUB_ACCOUNT}:bucket/*/table/*\"]},
                {\"Sid\":\"LF\",\"Effect\":\"Allow\",\"Action\":\"lakeformation:GetDataAccess\",\"Resource\":\"*\"},
                {\"Sid\":\"S3Errors\",\"Effect\":\"Allow\",\"Action\":[\"s3:AbortMultipartUpload\",\"s3:GetBucketLocation\",\"s3:GetObject\",\"s3:ListBucket\",\"s3:ListBucketMultipartUploads\",\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::${PRIMARY_BUCKET:-central-logs-${HUB_ACCOUNT}-${PRIMARY_REGION}}\",\"arn:aws:s3:::${PRIMARY_BUCKET:-central-logs-${HUB_ACCOUNT}-${PRIMARY_REGION}}/*\"]},
                {\"Sid\":\"Logs\",\"Effect\":\"Allow\",\"Action\":\"logs:PutLogEvents\",\"Resource\":\"arn:aws:logs:${PRIMARY_REGION}:${HUB_ACCOUNT}:log-group:/aws/kinesisfirehose/*\"}
              ]
            }"
            echo "=== Waiting 10s for IAM propagation ==="
            sleep 10
        fi

        run_cdk "$PRIMARY_PROFILE" "$PRIMARY_REGION" deploy \
            -c target=hub $SPOKE_CTX \
            $PARAMS --require-approval never "$@"

        # Save outputs to .env.deploy
        cat > "$ENV_FILE" <<EOF
# Auto-generated by deploy.sh
PRIMARY_PROFILE="${PRIMARY_PROFILE}"
PRIMARY_REGION="${PRIMARY_REGION}"
HUB_ACCOUNT="${HUB_ACCOUNT}"
USAGE_STATS_TABLE="BedrockInvocationAnalytics-usage-stats"
MODEL_PRICING_TABLE="BedrockInvocationAnalytics-model-pricing"
ATHENA_WORKGROUP="BedrockInvocationAnalytics-compute"
ICEBERG_CATALOG="s3tablescatalog/bedrock-analytics-${HUB_ACCOUNT}"
ICEBERG_DATABASE="bedrock_analytics"
ICEBERG_TABLE="usage_events"
SPOKE_WRITE_ROLE_ARN="arn:aws:iam::${HUB_ACCOUNT}:role/BedrockAnalytics-SpokeWriteRole"
ADMIN_USER="${WEBUI_USER}"
ADMIN_PASS="${WEBUI_PASS}"
STORAGE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
EOF
        echo "=== Hub deployed. Config saved to .env.deploy ==="
        ;;

    spoke)
        TARGET_PROFILE="${1:-}"
        HUB_ACCOUNT=$(get_account_id "$PRIMARY_PROFILE")

        SPOKES=()
        IFS='|' read -ra SPOKES <<< "$SPOKE_LIST"

        for spoke_info in "${SPOKES[@]}"; do
            IFS=: read -r profile region bucket <<< "$spoke_info"
            [[ -n "$TARGET_PROFILE" && "$profile" != "$TARGET_PROFILE" ]] && continue

            # Auto-bootstrap if needed
            if ! aws cloudformation describe-stacks --profile "$profile" --region "$region" \
                --stack-name CDKToolkit &>/dev/null; then
                echo "=== Bootstrapping CDK (${profile} / ${region}) ==="
                run_cdk "$profile" "$region" bootstrap
            fi

            STACK_ID="BedrockAnalytics-Spoke-${profile}-${region}"
            echo "=== Deploying Spoke (${profile} / ${region}) ==="
            PARAMS="--parameters ${STACK_ID}:LogPrefix=${LOG_PREFIX}"
            [[ -n "$bucket" ]] && PARAMS="$PARAMS --parameters ${STACK_ID}:ExistingBucketName=${bucket}"

            run_cdk "$profile" "$region" deploy "$STACK_ID" \
                -c target="spoke:${profile}" \
                -c hub_account="$HUB_ACCOUNT" \
                $PARAMS --require-approval never "$@"

            echo "=== Spoke ${profile}/${region} deployed ==="
        done
        ;;

    all)
        "$SCRIPT_DIR/deploy.sh" hub "$@"
        "$SCRIPT_DIR/deploy.sh" spoke "$@"
        ;;

    *)
        # Raw CDK command (diff, synth, destroy, etc.)
        run_cdk "$PRIMARY_PROFILE" "$PRIMARY_REGION" "$CMD" "$@"
        # Clean up pre-created Firehose role on destroy
        if [[ "$CMD" == "destroy" ]]; then
            ROLE="BedrockInvocationAnalytics-FirehoseDeliveryRole"
            aws iam delete-role-policy --profile "$PRIMARY_PROFILE" --role-name "$ROLE" --policy-name delivery 2>/dev/null || true
            aws iam delete-role --profile "$PRIMARY_PROFILE" --role-name "$ROLE" 2>/dev/null || true
            echo "=== Firehose role cleaned up ==="
        fi
        ;;
esac
