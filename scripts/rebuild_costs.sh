#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Recompute HOURLY / DAILY / MONTHLY rows from the Iceberg event log with current prices.
# Usage: scripts/rebuild_costs.sh START END [--dry-run]     (UTC dates, inclusive)
#        scripts/rebuild_costs.sh --all [--dry-run]         (first complete Iceberg day .. yesterday)
# Runs inside the compute-cost Lambda (it holds the L2 lock); resumes month by month until done.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$SCRIPT_DIR/.env.deploy" ]] && source "$SCRIPT_DIR/.env.deploy"
FN="BedrockInvocationAnalytics-compute-cost"
AWS=(aws --profile "${PRIMARY_PROFILE:?}" --region "${PRIMARY_REGION:?}" --cli-read-timeout 630)

DRY=false; ARGS=()
for a in "$@"; do [[ "$a" == "--dry-run" ]] && DRY=true || ARGS+=("$a"); done
if [[ "${ARGS[0]:-}" == "--all" ]]; then START="all"; END=""
else START="${ARGS[0]:?START_DATE or --all}"; END="${ARGS[1]:?END_DATE}"; fi

OUT=$(mktemp); trap 'rm -f "$OUT"' EXIT
invoke() {  # payload → prints result, retries on LockBusy
    local delay=10
    for _ in 1 2 3 4 5 6; do
        err=$("${AWS[@]}" lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out \
              --payload "$1" "$OUT" --query FunctionError --output text)
        if [[ "$err" == "None" ]]; then cat "$OUT"; return 0; fi
        if grep -q LockBusy "$OUT"; then echo "lock busy, retrying in ${delay}s" >&2; sleep $delay; delay=$((delay * 2)); continue; fi
        echo "FAILED: $(cat "$OUT")" >&2; return 1
    done
    echo "gave up: lock stayed busy" >&2; return 1
}

RESUME=""
while :; do
    result=$(invoke "{\"rebuild\": {\"start\": \"$START\", \"end\": \"$END\", \"dry_run\": $DRY, \"resume\": \"$RESUME\"}}")
    python3 - "$result" "$DRY" <<'PY'
import json, sys
r, dry = json.loads(sys.argv[1]), sys.argv[2] == "true"
print(f"days={r.get('days')} puts={r.get('puts')} deletes={r.get('deletes')} next={r.get('next')}")
if dry:
    for d in r.get("diffs", []):
        flag = "  DROP" if d["drop"] else ""
        print(f"  {d['pk'].split('#')[0][-4:]}.. {d['period']} invocations {d['invocations_before']} -> {d['invocations_after']}{flag}")
PY
    next=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("next") or "")' "$result")
    [[ -z "$next" ]] && break
    if [[ -n "$RESUME" && ! "$next" > "$RESUME" ]]; then
        echo "No progress: month $next does not fit in one invocation. Raise the Lambda timeout." >&2
        exit 1
    fi
    RESUME="$next"
    $DRY || invoke '{}' >/dev/null || true   # let incremental L2 run between months
done
