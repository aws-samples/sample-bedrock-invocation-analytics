#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build the WebUI Lambda package into build/webui-lambda: API code, ARM64 dependencies and the SPA under static/.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/build/webui-lambda"

(cd "$ROOT/webui" && npm ci --no-fund --no-audit --loglevel=error && npm run build)

rm -rf "$OUT" && mkdir -p "$OUT"
uv pip install --quiet --target "$OUT" --python-version 3.13 --python-platform aarch64-manylinux_2_28 \
    --only-binary :all: -r "$ROOT/webui/api/requirements.txt"
# boto3 ships with the Lambda runtime
rm -rf "$OUT"/boto3* "$OUT"/botocore* "$OUT"/s3transfer* "$OUT"/bin
cp "$ROOT"/webui/api/*.py "$OUT"/
cp -r "$ROOT/webui/dist" "$OUT/static"
echo "WebUI Lambda package: $OUT ($(du -sh "$OUT" | cut -f1))"
