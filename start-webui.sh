#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Start Bedrock Analytics WebUI
# Usage: ./start-webui.sh [--profile PROFILE] [--region REGION]
# Reads defaults from .env.deploy if available.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$SCRIPT_DIR/.env.deploy" ]] && set -a && source "$SCRIPT_DIR/.env.deploy" && set +a

# Defaults from .env.deploy (new format uses PRIMARY_*)
PROFILE="${PRIMARY_PROFILE:-$PROFILE}"
REGION="${PRIMARY_REGION:-$REGION}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --profile) PROFILE="$2"; shift 2 ;;
        --region)  REGION="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

export AWS_DEFAULT_REGION="${REGION:-us-west-2}"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# Detect host's IANA timezone so the WebUI's "Local" option matches the user's wall clock.
# Honors WEBUI_TIMEZONE if pre-set (e.g. via .env.deploy). Falls back to UTC if undetectable.
if [[ -z "${WEBUI_TIMEZONE:-}" ]]; then
    if command -v timedatectl >/dev/null 2>&1; then
        WEBUI_TIMEZONE=$(timedatectl show -p Timezone --value 2>/dev/null)
    fi
    if [[ -z "$WEBUI_TIMEZONE" && -L /etc/localtime ]]; then
        # /etc/localtime → /usr/share/zoneinfo/Asia/Shanghai → Asia/Shanghai
        WEBUI_TIMEZONE=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
    fi
    if [[ -z "$WEBUI_TIMEZONE" && -r /etc/timezone ]]; then
        WEBUI_TIMEZONE=$(cat /etc/timezone)
    fi
fi
export WEBUI_TIMEZONE="${WEBUI_TIMEZONE:-UTC}"
echo "WebUI timezone: $WEBUI_TIMEZONE"

cd "$SCRIPT_DIR" && uv run python -m webui.main
