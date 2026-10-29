# Bedrock Invocation Analytics

English | [中文](docs/README_CN.md)

Real-time analytics for Amazon Bedrock — monitor token usage, costs, and performance across AWS accounts.

> ⚠️ This sample is for demonstration purposes only and is not intended for production use. Use at your own risk.

## Features
- Summary cards: invocations, input/output tokens, cache tokens, estimated cost, avg latency, avg TPOT
- Token usage & cost by model and by caller (chart / pie / table views) with per-token-type cost breakdown (input / output / cache read / cache write)
- Performance: latency by model (min/avg/max) + latency trend with model selector
- TPOT by model (min/avg/max) + TTFT trend from CloudWatch (avg/p99)
- Usage trend over time with paired model/caller filter
- "Data up to" timestamp in header — reflects actual data freshness (L2 checkpoint), not UI refresh time
- Auto refresh (1min / 5min)
- Time-aware pricing: correct historical costs even if prices change; separate 5min vs 1h prompt cache rates; per-request pricing for rerank and image models
- Unpriced models are flagged in the header and alarmed; `scripts/rebuild_costs.sh` reprices history after a price is added
- CloudWatch alarms (SNS) on Lambda errors, DLQs, Firehose delivery, L2 lag and unpriced usage
- Pricing settings page: view/edit model pricing with history, weekly auto-sync from LiteLLM
- Ad-hoc Athena queries on the raw Iceberg event log for deep investigation
- Multi-account, multi-region support: account and region filters in a collapsible side panel (collapse it to hide them for screenshots), friendly account names from `config.yaml`
- Calendar presets (today, this week/month/quarter/year), rolling windows and custom date ranges, in local time or UTC
- Cognito sign-in (a new user pool or an existing one, in any region of the account); members of an admin group edit pricing, everyone else is read-only
- Internal ALB by default, optional CloudFront in front of it for HTTPS access from anywhere
- Responsive layout (desktop & mobile)

**Screenshot**

![WebUI](docs/webui_screenshot.png)

## Project Structure

```
├── deploy/
│   ├── cdk.json              # CDK config
│   ├── app.py                # CDK app entry (hub/spoke routing)
│   ├── hub_stack.py          # Primary account stack
│   ├── spoke_stack.py        # Spoke account stack
│   └── lambda/
│       ├── parse_log.py      # L1: S3 event → normalized JSON → Firehose
│       ├── compute_cost.py   # L2: Athena (Iceberg) → pricing → DynamoDB
│       ├── pricing.py        # Price versions and the SQL price table
│       └── sync_pricing.py   # Weekly pricing sync from LiteLLM
│   ├── common.py             # Shared constructs (log bucket, Lambda, alarms)
│   └── custom_resources/     # Bedrock logging, Lake Formation admin, IAM wait
│   └── webui.py              # WebUI: VPC, internal ALB, Lambda target, CloudFront, Cognito
├── webui/                    # React SPA (Vite, TypeScript, Cloudscape, ECharts)
│   ├── src/                  # Pages, charts, sign-in, filters
│   └── api/                  # Lambda behind the ALB: JSON API + SPA files
├── scripts/
│   ├── seed_pricing.py       # Seed pricing from LiteLLM
│   ├── rebuild_costs.sh      # Reprice past usage from Iceberg
│   ├── reconcile.py          # Read-only check: DynamoDB aggregates vs Iceberg
│   └── build_webui.sh        # Build the SPA and the WebUI Lambda package
├── tests/                    # Unit tests (uv run pytest)
├── config.example.yaml       # Multi-account deployment config
├── deploy.sh                 # CDK deploy script (hub/spoke/all/destroy)
└── pyproject.toml            # Dependencies (managed by uv)
```

## Architecture

Two-stage pipeline: **L1** (parse) turns every Bedrock call into a structured event in an Iceberg table; **L2** (compute) rolls those events up by pricing into DynamoDB for the dashboard to read.

![Architecture](docs/architecture-v3.png)

<details>
<summary>ASCII version (for AI/text access)</summary>

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Primary Account (Hub)                                                        │
│                                                                              │
│  S3 logs ──→ EventBridge ──→ Lambda: parse_log ──→ Firehose ──→ S3 Tables    │
│  (Bedrock)                   [L1: structure only]  (60s buffer) (Iceberg)    │
│                                                           │                  │
│                                  ┌────────────────────────┤                  │
│                                  │                        │                  │
│                                  ▼ every 5 min + hourly   │                  │
│                    Lambda: compute_cost                   │                  │
│                    [L2: pricing + aggregation]            │                  │
│                          │                                │                  │
│                          ▼                                ▼                  │
│                    DynamoDB: usage-stats          Athena: ad-hoc query       │
│                    (serving layer for UI)         (via Glue federation)      │
│                          │                                                   │
│                          ▼                                                   │
│     WebUI Lambda ◄── internal ALB ◄── (optional) CloudFront ◄── browser      │
│     (API + SPA)          ▲ intranet                    Cognito sign-in       │
│                                                                              │
│  DynamoDB: model-pricing ◄── Lambda: sync_pricing (weekly)                   │
│  IAM Role: SpokeWriteRole (assumed by spokes to write hub Firehose)          │
└──────────────────────────────────────────────────────────────────────────────┘
       ▲ assume role + cross-account firehose:PutRecord
       │
┌──────┴───────────────────────────────────────────────────────────────────────┐
│ Spoke Account(s)                                                             │
│                                                                              │
│  S3 logs ──→ EventBridge ──→ Lambda: parse_log ──→ Hub Firehose              │
│  (Bedrock)                                                                   │
└──────────────────────────────────────────────────────────────────────────────┘
```
</details>

**How it works:**

1. **Bedrock logs** land in each account's own S3 bucket (Bedrock requires same-account/region sinks).
2. **L1 `parse_log`** (per-account Lambda, triggered by S3 events) normalizes each record into a flat JSON event (account, region, model, caller, token counts, cache split, latency, error code) and hands it to Hub's Firehose via `PutRecordBatch`. Spoke Lambdas assume a cross-account role.
3. **Firehose → S3 Tables**: Firehose buffers ~60s then appends to the Iceberg table `bedrock_analytics.usage_events`, the **source of truth** for every Bedrock call, also directly queryable via Athena. The table is append-only, so a retried delivery can add a duplicate row; every reader deduplicates by `request_id`.
4. **L2 `compute_cost`** (hub only) never adds to a counter. Each run recomputes whole periods in one Athena query (dedup by `request_id`, time-aware prices, 5m vs 1h prompt cache split) and replaces that period's rows in DynamoDB, so reruns and overlaps are harmless:
   - **incremental** (every `cost_agg_interval_min`): recomputes the hours that received new events since its checkpoint, plus the current and previous hour. A failed run keeps the checkpoint.
   - **settle** (hourly): recomputes the last 2 completed days, days that got late events, and days whose count no longer matches Iceberg (daily check over 90 days); writes DAILY rows, and MONTHLY rows once a month is complete. `META/SETTLED#<account>#<region>` records the last day settled; the dashboard reads DAILY up to it and HOURLY after it.
   - **rebuild** (`scripts/rebuild_costs.sh`): the same recompute over any range, for repricing.
   A lease lock keeps the three from running at once. Separating compute from parse means a pricing fix only needs a rebuild over Iceberg, no re-parsing of raw S3.
5. **`sync_pricing`** pulls the latest model prices from [LiteLLM](https://github.com/BerriAI/litellm) weekly.
6. **WebUI**: a React SPA and a JSON API served by one Lambda behind an internal ALB (optionally fronted by CloudFront through a VPC origin). It reads DynamoDB (sub-second) as a serving-layer cache and checks Cognito access tokens on every API call. The header shows *"Data up to X"* based on L2's checkpoint, so users can tell real-time data freshness apart from UI refresh time.

## Prerequisites

- [AWS CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html) (`npm install -g aws-cdk`)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Node.js](https://nodejs.org/) 22 or 24 (builds the WebUI)
- AWS credentials configured (`aws configure` or `~/.aws/credentials`)

## Deploy

Copy `config.example.yaml` to `config.yaml` and fill in your AWS profiles, regions, account names, and optionally `alerts.email` for alarm notifications (confirm the SNS subscription email). The account marked `primary: true` deploys the full hub stack (DynamoDB, Iceberg, Firehose, WebUI); others deploy a lightweight spoke that forwards events to the hub.

```bash
# Install dependencies
uv sync

# Deploy primary account (auto-bootstraps CDK if needed)
./deploy.sh hub

# Deploy spoke account(s)
./deploy.sh spoke              # all spokes
./deploy.sh spoke lab          # specific spoke

# Deploy everything (recommended for updates)
./deploy.sh all
```

> **Note:** After code updates, use `./deploy.sh all` to ensure both hub and spoke Lambdas are updated. CDK prompts before IAM/security changes; set `CDK_REQUIRE_APPROVAL=never` for unattended runs.
>
> If the account already has Bedrock invocation logging pointing at a different S3 location, the deploy fails rather than overwrite it. Set `bucket` in `config.yaml` to that bucket (and `data.bedrock_log_prefix` to its prefix) to reuse it.

> For existing buckets, enable S3 EventBridge notifications:
> ```bash
> aws s3api put-bucket-notification-configuration --bucket YOUR_BUCKET \
>   --notification-configuration '{"EventBridgeConfiguration": {}}'
> ```

### Deployed Resources

**Primary account (Hub):**

| Resource | Purpose |
|----------|---------|
| S3 Bucket (optional) | Raw Bedrock invocation logs (SSE-KMS, TLS-only, lifecycle) |
| S3 ops bucket | Athena results (7-day expiry) and Firehose error backups (90-day expiry) |
| KMS key | Raw-log bucket encryption and alarm topic |
| Custom Resources | Bedrock invocation logging (merges with existing config, never overwrites another destination); Lake Formation admin for the CDK deploy role (adds to the admin list instead of replacing it) |
| DynamoDB × 2 | `usage-stats` (HOURLY / DAILY / MONTHLY aggregates + META) and `model-pricing` (time-aware); PITR and deletion protection |
| S3 Tables bucket + namespace + Iceberg table | `usage_events` — source of truth, queryable via Athena |
| Glue Data Catalog (federated) | `s3tablescatalog` pointing at the S3 Tables bucket |
| Firehose delivery stream | S3 Tables destination (append), 60s buffer |
| Athena workgroup | For `compute_cost` and ad-hoc queries |
| Lambda | `parse_log` (L1), `compute_cost` (L2), `sync_pricing`, custom-resource handlers; 1-month log retention |
| EventBridge rules | S3 trigger, L2 incremental and hourly settle, weekly pricing sync |
| SQS DLQ | Failed `parse_log` invocations and events; failed `compute_cost` runs (they resume on their own; purge after review) |
| SNS topic + CloudWatch alarms | Lambda errors, DLQ depth, Firehose failed rows / freshness, L2 checkpoint lag, settle lag, unpriced usage |
| IAM Roles | Firehose delivery role, Lambda execution roles, `SpokeWriteRole` (Firehose put only, trusted for the spoke `parse_log` role) |
| WebUI | VPC (created unless `webui.vpc_id` is set; no NAT), internal ALB with a Lambda target, optional CloudFront distribution (VPC origin), Cognito user pool (unless an existing one is given) and app client |

**Spoke accounts:**

| Resource | Purpose |
|----------|---------|
| S3 Bucket (optional) | Raw Bedrock logs (SSE-KMS, TLS-only) |
| Custom Resource | Configures Bedrock invocation logging |
| Lambda | `parse_log` (L1, role `BedrockAnalytics-SpokeParseLog-<region>`, assumes the hub role to `firehose:PutRecordBatch`) |
| EventBridge | S3 trigger with DLQ and retry |
| SQS DLQ | Dead-letter queue for failed processing |
| KMS key, SNS topic + alarms | `parse_log` errors and DLQ depth |

## Seed Pricing Data

Pricing data is sourced from [LiteLLM](https://github.com/BerriAI/litellm) (300+ Bedrock chat, embedding, rerank and image models). The weekly sync does the same automatically; seeding only fills a fresh table sooner:

```bash
AWS_DEFAULT_REGION=us-west-2 python3 scripts/seed_pricing.py \
  BedrockInvocationAnalytics-model-pricing YOUR_PROFILE
```

## Reprice Past Usage

After adding or correcting a price (Pricing page or LiteLLM sync), recompute the affected days from the Iceberg event log:

```bash
scripts/rebuild_costs.sh 2026-07-01 2026-07-31   # UTC dates, inclusive
scripts/rebuild_costs.sh --all --dry-run         # every day Iceberg fully covers; prints the changes
scripts/rebuild_costs.sh --all
```

The script runs inside the compute-cost Lambda month by month. `--dry-run` lists per-day invocation changes and flags drops. Today is left to the incremental and settle runs.

To check that DynamoDB matches Iceberg (invocations, tokens, latency, cost, TPOT, rollup invariants), run the read-only `AWS_PROFILE=... uv run python scripts/reconcile.py [START END]`.

## WebUI

`./deploy.sh hub` builds the SPA and deploys it with the hub stack. Choose how users reach it in the `webui` section of `config.yaml`:

| Setting | Effect |
|---|---|
| `cloudfront: true` | Adds a CloudFront distribution in front of the internal ALB (VPC origin). Open the `WebUiUrl` output (`https://<id>.cloudfront.net`). The ALB itself is never internet-facing. |
| `allowed_cidrs` | Intranet ranges (VPN, office, peered VPCs) allowed to reach the internal ALB directly at `WebUiAlbDnsName`. |
| `certificate_arn` | ACM certificate for HTTPS on the ALB (port 443). Without it, direct ALB access is plaintext HTTP on port 80 and deploy prints a warning. |
| `vpc_id`, `subnet_ids` | Use your VPC and two or more private subnets in different AZs. With CloudFront the VPC needs an internet gateway. Empty = a minimal VPC is created. |
| `cognito_user_pool_id` | Reuse an existing pool in the same account (any region); the stack only adds an app client. Empty = a pool is created. |
| `admin_group` | Group whose members can edit pricing. Every other signed-in user is read-only. Empty = the stack creates `bedrock-analytics-admin`. |
| `initial_admin_email` | Created pool only: creates the first admin and emails a temporary password. |

Either `cloudfront: true` or `allowed_cidrs` is required. WAF isn't managed by the stack; attach a web ACL to the ALB or the distribution if you need one.

Sign in with a username or email. The SPA uses `USER_PASSWORD_AUTH` (over TLS) so an existing pool's user-migration trigger keeps working.

Local development against a deployed stack:

```bash
cd webui && npm ci
WEBUI_URL=https://<id>.cloudfront.net npm run dev   # proxies /api and /config.json
```

## Tests

```bash
uv run pytest
```

## Cleanup

```bash
./deploy.sh destroy              # destroy hub stack
```

> DynamoDB tables, S3 buckets, the S3 Tables bucket and KMS keys are retained after stack deletion (RemovalPolicy: RETAIN). Bedrock invocation logging is removed only if the stack created it.

## Cost

| Service | Pricing | Notes |
|---------|---------|-------|
| Lambda | $0.20/M requests (ARM/Graviton) | L1 parse_log + L2 compute_cost |
| Firehose | $0.029/GB ingested + small per-record fee | Buffered then appended to Iceberg |
| S3 Tables (Iceberg) | ~$0.023/GB + $0.20/M requests | Partitioned by `account_id / day(ts)` |
| DynamoDB | Pay-per-request | Now only L2 aggregates write (≈13× fewer writes than V2 since L1 skips DDB) |
| Athena | $5/TB scanned | L2 scans one partition per run; ad-hoc queries extra |
| S3 (raw logs) | ~$0.023/GB/month | Auto-transitions to IA after 90 days |

**Monthly estimate** (1M Bedrock invocations, Anthropic models with caching):
- Lambda: ~$0.20 (L1) + ~$0.30 (L2: every 5 min plus hourly settle, 1 GB)
- KMS: $1 per key (one per account) + requests; CloudWatch alarms: ~$0.10 each (10 hub, 2 per spoke)
- Firehose + S3 Tables: ~$1 (few hundred MB of Iceberg data)
- DynamoDB: ~$0.30 (L2 writes only rows whose values changed)
- Athena: ~$0.30 (L2 scans recent day partitions; the daily 90-day count check reads only key columns; ad-hoc extra)
- S3 (raw logs): ~$1
- WebUI: ALB ~$16–18 + LCU; Lambda, CloudFront and Cognito well under $1 at dashboard traffic
- **Total: ~$24/month** for one hub and one spoke

Costs scale sub-linearly with invocations — Firehose buffering amortizes per-record overhead, and Iceberg partition pruning keeps Athena scans small as history grows.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
