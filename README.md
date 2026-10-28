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
- Multi-account, multi-region support (sidebar selector, friendly account names from `config.yaml`)
- Login authentication (configurable via `config.yaml`)
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
│       ├── aggregate_stats.py # Rollup: HOURLY → DAILY → MONTHLY
│       └── sync_pricing.py   # Weekly pricing sync from LiteLLM
│   ├── common.py             # Shared constructs (log bucket, Lambda, alarms)
│   └── custom_resources/     # Bedrock logging, Lake Formation admin, IAM wait
├── webui/
│   ├── main.py               # Entry point (ui.run)
│   ├── dashboard.py          # Dashboard page
│   ├── pricing.py            # Pricing settings page
│   ├── data.py               # DynamoDB data access
│   └── auth.py               # Login helpers
├── scripts/
│   ├── seed_pricing.py       # Seed pricing from LiteLLM
│   └── rebuild_costs.sh      # Reprice past usage from Iceberg
├── tests/                    # Unit tests (uv run pytest)
├── config.example.yaml       # Multi-account deployment config
├── deploy.sh                 # CDK deploy script (hub/spoke/all/destroy)
├── start-webui.sh            # WebUI launch script (reads .env.deploy)
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
│                                  ▼ every 5 min            │                  │
│                    Lambda: compute_cost                   │                  │
│                    [L2: pricing + aggregation]            │                  │
│                          │                                │                  │
│                          ▼                                ▼                  │
│                    DynamoDB: usage-stats          Athena: ad-hoc query       │
│                    (serving layer for UI)         (via Glue federation)      │
│                          │                                                   │
│                          ▼                                                   │
│                         WebUI                                                │
│                                                                              │
│  DynamoDB: model-pricing ◄── Lambda: sync_pricing (weekly)                   │
│  Lambda: aggregate_stats (HOURLY → DAILY → MONTHLY, daily/monthly)           │
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
3. **Firehose → S3 Tables**: Firehose buffers ~60s then upserts into the Iceberg table `bedrock_analytics.usage_events`, using `request_id` as the unique key — this is the **source of truth** for every Bedrock call, also directly queryable via Athena for ad-hoc investigation.
4. **L2 `compute_cost`** (hub only, EventBridge every `cost_agg_interval_min`) reads events ingested since its checkpoint from Iceberg via Athena, looks up time-aware pricing in DynamoDB, computes cost (splitting 5m vs 1h prompt cache), and aggregates into DynamoDB via `TransactWriteItems` with a dedup guard. A failed run keeps the checkpoint, so the next run retries. Separating compute from parse means a pricing fix only needs `scripts/rebuild_costs.sh` over Iceberg — no re-parsing raw S3.
5. **`aggregate_stats`** rolls hourly → daily → monthly on schedule, recomputing recent days so late events are included. **`sync_pricing`** pulls the latest model prices from [LiteLLM](https://github.com/BerriAI/litellm) weekly.
6. **WebUI** reads DynamoDB (sub-second) as a serving-layer cache. The header shows *"Data up to X"* based on L2's checkpoint, so users can tell real-time data freshness apart from UI refresh time.

## Prerequisites

- [AWS CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html) (`npm install -g aws-cdk`)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
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
| DynamoDB × 2 | `usage-stats` (serving layer + DEDUP + META) and `model-pricing` (time-aware); PITR and deletion protection |
| S3 Tables bucket + namespace + Iceberg table | `usage_events` — source of truth, queryable via Athena |
| Glue Data Catalog (federated) | `s3tablescatalog` pointing at the S3 Tables bucket |
| Firehose delivery stream | S3 Tables destination, 60s buffer, `request_id` upsert key |
| Athena workgroup | For `compute_cost` and ad-hoc queries |
| Lambda | `parse_log` (L1), `compute_cost` (L2), `aggregate_stats`, `sync_pricing`, custom-resource handlers; 1-month log retention |
| EventBridge rules | S3 trigger, L2 schedule, daily & monthly rollup, weekly pricing sync |
| SQS DLQ | Failed `parse_log` invocations and events |
| SNS topic + CloudWatch alarms | Lambda errors, DLQ depth, Firehose failed rows / freshness, L2 checkpoint lag, unpriced usage |
| IAM Roles | Firehose delivery role, Lambda execution roles, `SpokeWriteRole` (Firehose put only, trusted for the spoke `parse_log` role) |

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
```

The most recent ~25 hours are still owned by the incremental L2 run and are skipped; run the script again later to cover them.

## Start WebUI

```bash
./start-webui.sh
```

Open http://localhost:8060 in your browser. The server listens on 127.0.0.1 only; to reach it from another machine use SSH or SSM port forwarding, or set `WEBUI_HOST` behind a TLS reverse proxy.

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
| Lambda | $0.20/M requests (ARM/Graviton) | L1 parse_log + L2 compute_cost + rollups |
| Firehose | $0.029/GB ingested + small per-record fee | Buffered then Iceberg upsert |
| S3 Tables (Iceberg) | ~$0.023/GB + $0.20/M requests | Partitioned by `account_id / day(ts)` |
| DynamoDB | Pay-per-request | Now only L2 aggregates write (≈13× fewer writes than V2 since L1 skips DDB) |
| Athena | $5/TB scanned | L2 scans one partition per run; ad-hoc queries extra |
| S3 (raw logs) | ~$0.023/GB/month | Auto-transitions to IA after 90 days |

**Monthly estimate** (1M Bedrock invocations, Anthropic models with caching):
- Lambda: ~$0.20 (L1) + ~$0.05 (L2, every 5 min)
- KMS: $1 per key (one per account) + requests; CloudWatch alarms: ~$0.10 each (9 hub, 2 per spoke)
- Firehose + S3 Tables: ~$1 (few hundred MB of Iceberg data)
- DynamoDB: ~$0.30 (L2 TransactWriteItems, ~3 items/event)
- Athena: ~$0.10 (L2 scans small hourly partitions; ad-hoc extra)
- S3 (raw logs): ~$1
- **Total: ~$6/month** for one hub and one spoke

Costs scale sub-linearly with invocations — Firehose buffering amortizes per-record overhead, and Iceberg partition pruning keeps Athena scans small as history grows.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
