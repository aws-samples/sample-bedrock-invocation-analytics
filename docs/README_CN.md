# Bedrock 调用日志分析

[English](../README.md) | 中文

Amazon Bedrock 实时分析 — 监控多账户的 Token 用量、成本和性能。

> ⚠️ 本示例仅用于演示用途，不适合用于生产环境。使用风险自负。

## 功能
- 概览卡片：调用次数、输入/输出 Token、缓存 Token、预估成本、平均延迟、平均 TPOT
- 按模型和调用者的 Token 用量与成本（图表 / 饼图 / 表格视图），成本按 Token 类型分拆（输入 / 输出 / 缓存读 / 缓存写)
- 性能分析：按模型延迟（min/avg/max）+ 延迟趋势（支持按模型筛选）
- TPOT 分析：按模型 TPOT（min/avg/max）+ CloudWatch TTFT 趋势（avg/p99）
- 使用趋势（支持模型/调用者联动筛选）
- Header 显示 "Data up to"（基于 L2 checkpoint），反映真实数据新鲜度而非 UI 刷新时间
- 自动刷新（1min / 5min）
- 时间感知定价：价格变化后历史成本仍然正确；区分 5min 与 1h prompt cache 单价；rerank 与图像模型按次计价
- 未定价模型在 Header 中提示并触发告警；补价后管理员可在定价页面重算历史用量（预览、进度、取消）
- CloudWatch 告警（SNS）：Lambda 错误、DLQ、Firehose 投递、L2 延迟、未定价调用
- 定价设置页面：查看/编辑模型定价及历史，每周从 LiteLLM 自动同步
- Athena 即席查询 Iceberg 事件表，支持深度排查
- 多账户、多区域支持：账号和区域筛选放在可折叠侧栏里（截图时收起即可隐藏），友好账号名来自 `config.yaml`
- 日历预设（今天、本周/本月/本季度/本年）、滚动窗口和自定义日期范围，可按本地时区或 UTC 显示
- Cognito 登录（新建 user pool，或沿用本账号任意区域的已有 pool）；管理员组成员可编辑定价，其他人只读
- 默认部署内部 ALB，可选在前面加 CloudFront，从任何地方通过 HTTPS 访问
- 响应式布局（桌面和移动端）

**截图**

![WebUI](webui_screenshot.png)

## 项目结构

```
├── deploy/
│   ├── cdk.json              # CDK 配置
│   ├── app.py                # CDK 应用入口（hub/spoke 路由）
│   ├── hub_stack.py          # 主账号 Stack
│   ├── spoke_stack.py        # Spoke 账号 Stack
│   └── lambda/
│       ├── parse_log.py      # L1：S3 事件 → 规整 JSON → Firehose
│       ├── compute_cost.py   # L2：Athena (Iceberg) → 定价 → DynamoDB
│       └── sync_pricing.py   # 每周从 LiteLLM 同步定价
│   ├── common.py             # 共用构件（日志桶、Lambda、告警）
│   └── custom_resources/     # Bedrock 日志、Lake Formation 管理员、IAM 等待
│   └── webui.py              # WebUI：VPC、内部 ALB、Lambda 目标、CloudFront、Cognito
├── webui/                    # React SPA（Vite、TypeScript、Cloudscape、ECharts）
│   ├── src/                  # 页面、图表、登录、筛选
│   └── api/                  # ALB 后面的 Lambda：JSON API + SPA 静态文件
├── scripts/
│   ├── seed_pricing.py       # 从 LiteLLM 导入定价
│   ├── rebuild_costs.sh      # 基于 Iceberg 重算历史成本
│   ├── reconcile.py          # 只读对账：DynamoDB 汇总 vs Iceberg
│   └── build_webui.sh        # 构建 SPA 和 WebUI Lambda 部署包
├── tests/                    # 单元测试（uv run pytest）
├── config.example.yaml       # 多账号部署配置
├── deploy.sh                 # CDK 部署脚本（hub/spoke/all/destroy）
└── pyproject.toml            # 依赖管理（uv）
```

## 架构

两阶段管道：**L1**（解析）把每次 Bedrock 调用转成结构化事件写入 Iceberg 表;**L2**（计算）按定价汇总这些事件到 DynamoDB 供仪表盘读取。

![Architecture](architecture-v3.png)

<details>
<summary>ASCII 版本（方便 AI/文本访问）</summary>

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 主账号（Hub）                                                                  │
│                                                                              │
│  S3 日志 ──→ EventBridge ──→ Lambda: parse_log ──→ Firehose ──→ S3 Tables    │
│  (Bedrock)                   [L1：仅规整]         (60s 缓冲)    (Iceberg)    │
│                                                           │                  │
│                                  ┌────────────────────────┤                  │
│                                  │                        │                  │
│                                  ▼ 每 5 分钟 + 每小时     │                  │
│                    Lambda: compute_cost                   │                  │
│                    [L2：定价 + 聚合]                      │                  │
│                          │                                │                  │
│                          ▼                                ▼                  │
│                    DynamoDB: usage-stats          Athena：即席查询           │
│                    （UI 的 serving 缓存层）       （通过 Glue 联邦）          │
│                          │                                                   │
│                          ▼                                                   │
│     WebUI Lambda ◄── 内部 ALB ◄── （可选）CloudFront ◄── 浏览器              │
│     （API + SPA）        ▲ 内网                         Cognito 登录         │
│                                                                              │
│  DynamoDB: model-pricing ◄── Lambda: sync_pricing（每周）                     │
│  IAM Role: SpokeWriteRole（供 spoke assume 后写入 Hub Firehose）              │
└──────────────────────────────────────────────────────────────────────────────┘
       ▲ assume role + 跨账号 firehose:PutRecord
       │
┌──────┴───────────────────────────────────────────────────────────────────────┐
│ Spoke 账号                                                                   │
│                                                                              │
│  S3 日志 ──→ EventBridge ──→ Lambda: parse_log ──→ Hub Firehose              │
│  (Bedrock)                                                                   │
└──────────────────────────────────────────────────────────────────────────────┘
```
</details>

**工作原理：**

1. **Bedrock 日志** 落到各账号自己的 S3 桶（Bedrock 要求日志汇流桶与调用在同账号同 Region）。
2. **L1 `parse_log`**（每个账号一个 Lambda,由 S3 事件触发）把每条记录规整成扁平 JSON 事件（account、region、model、caller、token 数、缓存拆分、延迟、错误码）,通过 `PutRecordBatch` 投递到 Hub 的 Firehose。Spoke Lambda 通过跨账号角色访问。
3. **Firehose → S3 Tables**：Firehose 缓冲约 60 秒后追加写入 Iceberg 表 `bedrock_analytics.usage_events`，这是每次 Bedrock 调用的**真实数据源**，也可直接用 Athena 即席查询。该表只追加，投递重试可能产生重复行，所有读取方都按 `request_id` 去重。
4. **L2 `compute_cost`**（仅 Hub）从不做累加。每次运行用一条 Athena 查询重算整段时间（按 `request_id` 去重、时间感知定价、拆分 5min 与 1h prompt cache），并整体替换 DynamoDB 中该时段的行，所以重跑或时间重叠都不会出错：
   - **incremental**（每 `cost_agg_interval_min` 分钟）：重算自 checkpoint 以来有新事件的小时，以及当前和上一小时。失败时 checkpoint 不前进。
   - **settle**（每小时）：重算最近 2 个完整日、收到迟到事件的日期，以及与 Iceberg 条数不一致的日期（每天检查一次近 90 天）；写 DAILY，月份结束后写 MONTHLY。`META/SETTLED#<account>#<region>` 记录已结算到哪一天，Dashboard 在此之前读 DAILY，之后读 HOURLY。
   - **rebuild**（`scripts/rebuild_costs.sh`）：对任意区间做同样的重算，用于重新定价。
   - **重算任务**（定价页面 → Reprice usage）：从某天起或全部历史做同样的重算，由 incremental 分块推进，WebUI 显示进度与变化。
   三者由租约锁互斥。解析和计算分离，修正定价只需基于 Iceberg 重算，无需重新解析原始 S3。
5. **`sync_pricing`** 每周从 [LiteLLM](https://github.com/BerriAI/litellm) 拉最新模型定价。
6. **WebUI**：一个 Lambda 同时提供 React SPA 和 JSON API，部署在内部 ALB 后面（可选通过 VPC origin 在前面加 CloudFront）。它从 DynamoDB 读取数据（亚秒级响应），作为 serving 缓存层，每次 API 调用都校验 Cognito access token。Header 显示基于 L2 checkpoint 的 *"Data up to X"*,让用户能区分真实数据新鲜度和 UI 刷新时间。

## 前置条件

- [AWS CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html)（`npm install -g aws-cdk`）
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- [Node.js](https://nodejs.org/) 22 或 24（构建 WebUI）
- 已配置 AWS 凭证（`aws configure` 或 `~/.aws/credentials`）

## 部署

复制 `config.example.yaml` 为 `config.yaml`,填写 AWS profile、region、账号名,可选填 `alerts.email` 接收告警（部署后需确认 SNS 订阅邮件）。标记 `primary: true` 的为主账号,部署完整 Hub stack（DynamoDB、Iceberg、Firehose、WebUI）;其他账号部署轻量 Spoke,将事件转发到 Hub。

```bash
# 安装依赖
uv sync

# 部署主账号（自动初始化 CDK）
./deploy.sh hub

# 部署其他账号
./deploy.sh spoke              # 所有 spoke
./deploy.sh spoke lab          # 指定 spoke

# 全部部署（更新代码后推荐使用）
./deploy.sh all
```

> **注意：** 代码更新后，建议使用 `./deploy.sh all` 确保 hub 和 spoke 的 Lambda 同步更新。涉及 IAM/安全变更时 CDK 会请求确认;无人值守时设置 `CDK_REQUIRE_APPROVAL=never`。
>
> 如果账号已有指向其他 S3 位置的 Bedrock 调用日志配置,部署会失败而不会覆盖它。将 `config.yaml` 中的 `bucket`（以及 `data.bedrock_log_prefix`）设为该位置即可复用。

> 使用已有存储桶时，需启用 S3 EventBridge 通知：
> ```bash
> aws s3api put-bucket-notification-configuration --bucket 你的桶名 \
>   --notification-configuration '{"EventBridgeConfiguration": {}}'
> ```

### 部署资源

**主账号（Hub）：**

| 资源 | 用途 |
|------|------|
| S3 存储桶（可选） | 原始 Bedrock 调用日志（SSE-KMS、仅 TLS、生命周期） |
| S3 ops 存储桶 | Athena 结果（7 天过期）和 Firehose 错误备份（90 天过期） |
| KMS 密钥 | 原始日志桶加密和告警主题 |
| Custom Resource | Bedrock 调用日志（与现有配置合并,不覆盖其他目标）;Lake Formation 管理员（追加 CDK 部署角色,不替换管理员列表） |
| DynamoDB × 2 | `usage-stats`（HOURLY / DAILY / MONTHLY 汇总 + META）和 `model-pricing`（时间感知）;开启 PITR 与删除保护 |
| S3 Tables bucket + namespace + Iceberg 表 | `usage_events` — 真实数据源,通过 Athena 可查 |
| Glue Data Catalog（联邦） | `s3tablescatalog`,指向 S3 Tables bucket |
| Firehose 投递流 | S3 Tables 目标（追加写入）,60s 缓冲 |
| Athena workgroup | 供 `compute_cost` 和即席查询使用 |
| Lambda | `parse_log`（L1）、`compute_cost`（L2）、`sync_pricing`、Custom Resource 处理器;日志保留 1 个月 |
| EventBridge 规则 | S3 触发器、L2 incremental 与每小时 settle、每周定价同步 |
| SQS DLQ | `parse_log` 失败的调用和事件；`compute_cost` 失败的运行（会自行续跑，检查后清空） |
| SNS 主题 + CloudWatch 告警 | Lambda 错误、DLQ 深度、Firehose 失败行/新鲜度、L2 checkpoint 延迟、settle 延迟、未定价调用 |
| IAM Roles | Firehose 投递角色、Lambda 执行角色、`SpokeWriteRole`（仅 Firehose 写入,只信任 spoke 的 `parse_log` 角色） |
| WebUI | VPC（未指定 `webui.vpc_id` 时新建，不建 NAT）、带 Lambda 目标的内部 ALB、可选 CloudFront 分发（VPC origin）、Cognito user pool（未指定已有 pool 时新建）和 app client |

**Spoke 账号：**

| 资源 | 用途 |
|------|------|
| S3 存储桶（可选） | 原始 Bedrock 日志（SSE-KMS、仅 TLS） |
| Custom Resource | 配置 Bedrock 调用日志 |
| Lambda | `parse_log`（L1,角色 `BedrockAnalytics-SpokeParseLog-<region>`,assume hub 角色后调 `firehose:PutRecordBatch`） |
| EventBridge | S3 触发器,带 DLQ 与重试 |
| SQS DLQ | 处理失败的死信队列 |
| KMS 密钥、SNS 主题 + 告警 | `parse_log` 错误和 DLQ 深度 |

## 初始化定价数据

定价数据来源于 [LiteLLM](https://github.com/BerriAI/litellm)（覆盖 300+ Bedrock 对话、嵌入、rerank 和图像模型）。每周同步会自动完成同样的工作,手动导入只是让新表更快可用：

```bash
AWS_DEFAULT_REGION=us-west-2 python3 scripts/seed_pricing.py \
  BedrockInvocationAnalytics-model-pricing YOUR_PROFILE
```

## 重算历史成本

新增或修正价格后，管理员打开 **Pricing → Reprice usage**：选择起始日期（从该日所在月的 1 日起按整月重算，最后重算今天）或全部历史，先 **Preview** 查看每天、每月调用次数与成本的变化，再 **Reprice**。任务在后台按几分钟一块执行，可在月与月之间取消。

也可以在命令行同步执行同样的重算：

```bash
scripts/rebuild_costs.sh 2026-07-01 2026-07-31   # UTC 日期，含首尾
scripts/rebuild_costs.sh --all --dry-run         # Iceberg 完整覆盖的所有日期；只打印变化
scripts/rebuild_costs.sh --all
```

脚本在 compute-cost Lambda 内按月执行。`--dry-run` 列出每天调用次数的变化并标出下降项。当天由 incremental 与 settle 负责。

核对 DynamoDB 与 Iceberg 是否一致（调用次数、token、延迟、成本、TPOT、汇总不变式）：只读运行 `AWS_PROFILE=... uv run python scripts/reconcile.py [START END]`。

## WebUI

`./deploy.sh hub` 会构建 SPA 并随 hub 栈一起部署。用户的访问方式在 `config.yaml` 的 `webui` 段里配置：

| 配置 | 作用 |
|---|---|
| `cloudfront: true` | 在内部 ALB 前加一个 CloudFront 分发（VPC origin）。访问输出里的 `WebUiUrl`（`https://<id>.cloudfront.net`）。ALB 本身始终不暴露到公网。 |
| `allowed_cidrs` | 允许直接访问内部 ALB（`WebUiAlbDnsName`）的内网网段（VPN、办公网、对等 VPC）。 |
| `certificate_arn` | ALB 的 HTTPS 证书（443 端口）。不填时直连 ALB 走明文 HTTP 80 端口，部署时会给出警告。 |
| `vpc_id`、`subnet_ids` | 使用已有 VPC，以及至少两个不同可用区的私有子网。开 CloudFront 时 VPC 需要有 Internet Gateway。留空则新建一个最小 VPC。 |
| `cognito_user_pool_id` | 沿用本账号已有的 pool（任意区域），栈只在其中新增一个 app client。留空则新建 pool。 |
| `admin_group` | 可编辑定价的用户组，其他已登录用户只读。留空则由栈创建 `bedrock-analytics-admin`。 |
| `initial_admin_email` | 仅新建 pool 时生效：创建首个管理员，并把临时密码发到这个邮箱。 |

`cloudfront: true` 和 `allowed_cidrs` 至少要配一个。WAF 不由本项目管理，需要的话自行给 ALB 或 CloudFront 分发挂 Web ACL。

可以用用户名或邮箱登录。SPA 使用 `USER_PASSWORD_AUTH`（经 TLS 传输），这样沿用的 pool 如果挂了用户迁移触发器，也能正常工作。

本地开发时连接已部署的栈：

```bash
cd webui && npm ci
WEBUI_URL=https://<id>.cloudfront.net npm run dev   # 代理 /api 和 /config.json
```

## 测试

```bash
uv run pytest
```

## 清理

```bash
./deploy.sh destroy --profile YOUR_PROFILE
```

> DynamoDB 表、S3 存储桶、S3 Tables bucket 和 KMS 密钥在删除 Stack 后会保留（RemovalPolicy: RETAIN）。Bedrock 调用日志仅在由本 Stack 创建时才会被移除。

## 成本

| 服务 | 定价 | 说明 |
|------|------|------|
| Lambda | $0.20/百万次请求（ARM/Graviton） | L1 parse_log + L2 compute_cost + 汇总 |
| Firehose | $0.029/GB 摄入 + 少量每记录费用 | 缓冲后追加写入 Iceberg |
| S3 Tables (Iceberg) | 约 $0.023/GB + $0.20/百万次请求 | 按 `account_id / day(ts)` 分区 |
| DynamoDB | 按请求付费 | 只有 L2 聚合写入（较 V2 写入次数减少约 13 倍,L1 不再写 DDB） |
| Athena | $5/TB 扫描 | L2 每次扫描一个分区;即席查询另计 |
| S3（原始日志） | 约 $0.023/GB/月 | 90 天后自动转为低频存储 |

**月度估算**（100 万次 Bedrock 调用,Anthropic 模型含缓存）：
- Lambda：约 $0.20（L1）+ 约 $0.30（L2：每 5 分钟加每小时 settle，1 GB）
- Firehose + S3 Tables：约 $1（几百 MB Iceberg 数据）
- DynamoDB：约 $0.30（L2 只写值有变化的行）
- Athena：约 $0.30（L2 扫描近期日分区；每天一次的 90 天条数检查只读键列；即席查询另计）
- S3（原始日志）：约 $1
- KMS：每个密钥 $1（每账号一个）+ 请求费;CloudWatch 告警：每个约 $0.10（hub 10 个,每个 spoke 2 个）
- WebUI：ALB 约 $16–18 + LCU；Lambda、CloudFront、Cognito 在看板的访问量下都远低于 $1
- **合计：约 $24/月**（一个 hub + 一个 spoke）

成本对调用次数是亚线性增长 —— Firehose 缓冲摊薄每记录开销,Iceberg 分区裁剪随历史增长仍保持较小扫描量。

## 安全

参见 [CONTRIBUTING](../CONTRIBUTING.md#security-issue-notifications)。

## 许可证

本项目使用 MIT-0 许可证,参见 [LICENSE](../LICENSE) 文件。
