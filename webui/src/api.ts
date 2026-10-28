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

export interface Status {
  data_up_to: string | null
  unpriced_models: string[]
  pricing_sync: { synced_at?: string; models_updated?: number; models_added?: number; models_skipped?: number } | null
  user: { username: string; is_admin: boolean }
}

export const PRICE_FIELDS = ['input_per_1k', 'output_per_1k', 'cache_read_per_1k', 'cache_write_per_1k', 'cache_write_1h_per_1k', 'per_request'] as const
export type PriceField = (typeof PRICE_FIELDS)[number]

export type PricingRecord = Record<PriceField, number> & {
  model_id: string
  effective_date: string
  source: string
  updated_by: string
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
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
    throw new ApiError(resp.status, (body as { error?: string }).error ?? `HTTP ${resp.status}`)
  }
  return (resp.status === 204 ? undefined : await resp.json()) as T
}

export interface RangeParams {
  account: string
  start: string
  end: string
  tz: string
}

const qs = (params: Record<string, string>) => new URLSearchParams(params).toString()

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
  deletePricing: (model: string, effectiveDate: string) =>
    request<void>(`/api/pricing/${encodeURIComponent(model)}?${qs({ effective_date: effectiveDate })}`, { method: 'DELETE' }),
}
