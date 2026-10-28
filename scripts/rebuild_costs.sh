#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Reprice past usage from the Iceberg event log with current pricing, then refresh the rollups.
# Usage: scripts/rebuild_costs.sh START_DATE END_DATE   (UTC, YYYY-MM-DD, inclusive; profile/region from .env.deploy)
# The last ~25 hours are skipped (still owned by incremental L2); re-run later to cover them.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$SCRIPT_DIR/.env.deploy" ]] && source "$SCRIPT_DIR/.env.deploy"
START="${1:?START_DATE required}"
END="${2:?END_DATE required}"
STACK="BedrockInvocationAnalytics"
AWS=(aws --profile "${PRIMARY_PROFILE:?}" --region "${PRIMARY_REGION:?}" --cli-read-timeout 310)

invoke() {
    local fn="$1" payload="$2" out
    out=$(mktemp)
    "${AWS[@]}" lambda invoke --function-name "$fn" --cli-binary-format raw-in-base64-out \
        --payload "$payload" "$out" --query FunctionError --output text | grep -qv None \
        && { echo "FAILED $fn $payload: $(cat "$out")"; rm -f "$out"; exit 1; }
    cat "$out"; echo; rm -f "$out"
}

day="$START"
while [[ ! "$day" > "$END" ]]; do
    echo "== $day"
    invoke "$STACK-compute-cost" "{\"rebuild\": {\"start\": \"$day\"}}"
    invoke "$STACK-aggregate-stats" "{\"type\": \"daily\", \"date\": \"$day\"}" >/dev/null
    day=$(date -u -d "$day + 1 day" +%F)
done

month="${START:0:7}"
current_month=$(date -u +%Y-%m)
while [[ ! "$month" > "${END:0:7}" && "$month" < "$current_month" ]]; do
    echo "== monthly $month"
    invoke "$STACK-aggregate-stats" "{\"type\": \"monthly\", \"month\": \"$month\"}" >/dev/null
    month=$(date -u -d "$month-01 + 1 month" +%Y-%m)
done
