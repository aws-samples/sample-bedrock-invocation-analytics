import { fetchAuthSession } from 'aws-amplify/auth'

export interface Account {
  account_id: string
  region: string
  key: string
  name: string
}

export interface UsageRow {
  invocations: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  cost_input: number
  cost_output: number
  cost_cache_read: number
  cost_cache_write: number
  cost_request: number
}

export interface ModelRow extends UsageRow {
  model: string
  avg_latency_ms: number
  min_latency_ms: number
  max_latency_ms: number
  tpot_avg: number
  tpot_min: number
  tpot_max: number
}

export interface CallerRow extends UsageRow {
  caller: string
}

export interface TrendPoint extends UsageRow {
  period: string
  avg_latency_ms: number
  min_latency_ms: number
  max_latency_ms: number
}

export interface Summary {
  invocations: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  avg_latency_ms: number
  avg_tpot: number
}

export interface DashboardData {
  summary: Summary
  models: ModelRow[]
  callers: CallerRow[]
  trend_total: TrendPoint[]
}

export interface TtftPoint {
  timestamp: string
  ttft_avg: number
  ttft_p99: number
}

export type RepriceStatus = 'queued' | 'running' | 'cancelling' | 'done' | 'failed' | 'cancelled'

export interface RepriceDiff {
  pk: string
  period: string
  invocations_before: number
  invocations_after: number
  cost_before: number
  cost_after: number
  drop: boolean
}

export interface RepriceJob {
  job_id: string
  status: RepriceStatus
  start: string
  dry_run: boolean
  created_at: string
  started_at?: string
  updated_at?: string
  finished_at?: string
  next?: string
  months_done?: number
  months_total?: number
  today_done?: boolean
  stalls?: number
  totals?: { days: number; puts: number; deletes: number; drops: number; days_changed: number; months_changed: number }
  started_by?: string
  diffs?: RepriceDiff[]
  error?: string
}

export interface UnpricedModel {
  model: string
  kind: string
  id: string
  accounts: { key: string; name: string; region: string }[]
  first_seen?: string
  last_seen?: string
}

export const REPRICE_ACTIVE: RepriceStatus[] = ['queued', 'running', 'cancelling']

export interface Status {
  data_up_to: string | null
  reprice: Pick<RepriceJob, 'job_id' | 'status' | 'dry_run' | 'months_done' | 'months_total' | 'today_done'> | null
  unpriced_models: UnpricedModel[]
  pricing_sync: { synced_at?: string; models_updated?: number; models_added?: number; models_skipped?: number } | null
  user: { username: string; email: string; is_admin: boolean }
  admin_group: string
  data_sources: { region?: string; usage_table?: string; pricing_table?: string; athena_workgroup?: string; iceberg_table?: string }
}

export const PRICE_FIELDS = ['input_per_1k', 'output_per_1k', 'cache_read_per_1k', 'cache_write_per_1k', 'cache_write_1h_per_1k', 'per_request'] as const
export type PriceField = (typeof PRICE_FIELDS)[number]

export type PricingRecord = Record<PriceField, number> & {
  model_id: string
  effective_date: string
  source: string
  updated_by: string
  price_as?: string
  label?: string
  alias_broken?: boolean
}

/** ARN resource types billed per model unit and time, not per token: usage is counted, cost isn't estimated. */
export const NOT_TOKEN_BILLED = ['imported-model/', 'provisioned-model/']
export const isNotTokenBilled = (model: string) => NOT_TOKEN_BILLED.some((p) => model.startsWith(p))

export const MODEL_KINDS: Record<string, string> = {
  'application-inference-profile': 'Application inference profile',
  'imported-model': 'Imported model',
  'provisioned-model': 'Provisioned model',
  'custom-model': 'Custom model',
}

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = (await fetchAuthSession()).tokens?.accessToken?.toString()
  if (!token) throw new ApiError(401, 'Not signed in')
  const resp = await fetch(path, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${token}`, ...(init.body ? { 'Content-Type': 'application/json' } : {}) },
  })
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new ApiError(resp.status, (body as { error?: string }).error ?? `HTTP ${resp.status}`, body)
  }
  return (resp.status === 204 ? undefined : await resp.json()) as T
}

export interface RangeParams {
  account: string
  start: string
  end: string
  tz: string
}

// encodeURIComponent, not URLSearchParams: spaces must be %20 because the API decodes with plain percent-decoding.
const qs = (params: Record<string, string>) =>
  Object.entries(params).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&')

export const api = {
  accounts: () => request<{ accounts: Account[] }>('/api/accounts').then((r) => r.accounts),
  status: () => request<Status>('/api/status'),
  dashboard: (p: RangeParams) => request<DashboardData>(`/api/dashboard?${qs({ ...p })}`),
  trend: (p: RangeParams, dim: string) => request<{ trend: TrendPoint[] }>(`/api/trend?${qs({ ...p, dim })}`).then((r) => r.trend),
  ttft: (model: string, start: string, end: string) =>
    request<{ ttft: TtftPoint[] }>(`/api/ttft?${qs({ model, start, end })}`).then((r) => r.ttft),
  pricing: () => request<{ models: PricingRecord[] }>('/api/pricing').then((r) => r.models),
  pricingHistory: (model: string) =>
    request<{ history: PricingRecord[] }>(`/api/pricing/${encodeURIComponent(model)}`).then((r) => r.history),
  savePricing: (model: string, prices: Record<PriceField, number>, effectiveDate: string) =>
    request<void>(`/api/pricing/${encodeURIComponent(model)}`, {
      method: 'PUT',
      body: JSON.stringify({ ...prices, effective_date: effectiveDate }),
    }),
  reprice: () => request<{ job: RepriceJob | null }>('/api/reprice').then((r) => r.job),
  startReprice: (start: string, dryRun: boolean) =>
    request<{ job: RepriceJob }>('/api/reprice', { method: 'POST', body: JSON.stringify({ start, dry_run: dryRun }) }).then((r) => r.job),
  cancelReprice: (jobId: string) =>
    request<{ job: RepriceJob }>(`/api/reprice?${qs({ job_id: jobId })}`, { method: 'DELETE' }).then((r) => r.job),
  savePriceAlias: (model: string, priceAs: string, label: string) =>
    request<void>(`/api/pricing/${encodeURIComponent(model)}`, { method: 'PUT', body: JSON.stringify({ price_as: priceAs, label }) }),
  deletePricing: (model: string, effectiveDate: string) =>
    request<void>(`/api/pricing/${encodeURIComponent(model)}?${qs({ effective_date: effectiveDate })}`, { method: 'DELETE' }),
}
