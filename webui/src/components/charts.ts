import type { ModelRow, TrendPoint, TtftPoint, UsageRow } from '../api'
import { formatInTz, round4 } from '../format'
import type { ChartOption } from './EChart'

// One colour per token category; usage and cost of the same category share it, hatching marks the cost bars.
const COLOR = { input: '#5070dd', cache_read: '#b6d634', cache_write: '#f97317', output: '#01b7d4', request: '#9a60b4' }
const COST_DECAL = { symbol: 'line', dashArrayX: [1, 0], dashArrayY: [4, 3], rotation: -0.785, color: 'rgba(255,255,255,0.4)' }
const MIN_MAX = { min: '#10B981', avg: '#E879F9', max: '#8B5CF6' }

// Long names (callers are ARN paths) are truncated on the axis; the tooltip shows the full name.
const axisLabels = (labels: string[]) =>
  ({ type: 'category', data: labels, axisLabel: { rotate: 40, interval: 0, width: 160, overflow: 'truncate' } })

/** Three bars per row: input-side tokens (stacked with cache), output tokens, and a stacked cost bar on the right axis. */
export function usageCostBar(labels: string[], rows: UsageRow[]): ChartOption {
  const tokens = (name: string, key: keyof UsageRow, stack: string, color: string) =>
    ({ name, type: 'bar', stack, itemStyle: { color }, data: rows.map((r) => r[key]) })
  const cost = (name: string, key: keyof UsageRow, color: string) =>
    ({ name, type: 'bar', stack: 'cost', yAxisIndex: 1, itemStyle: { color, decal: COST_DECAL }, data: rows.map((r) => round4(r[key])) })
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0, type: 'scroll' },
    grid: { top: 40, bottom: 90, left: 70, right: 60 },
    xAxis: axisLabels(labels),
    yAxis: [{ type: 'value', name: 'Tokens' }, { type: 'value', name: 'Cost ($)' }],
    series: [
      tokens('Input Tokens', 'input_tokens', 'tok_in', COLOR.input),
      tokens('Cache Read Tokens', 'cache_read_tokens', 'tok_in', COLOR.cache_read),
      tokens('Cache Write Tokens', 'cache_write_tokens', 'tok_in', COLOR.cache_write),
      tokens('Output Tokens', 'output_tokens', 'tok_out', COLOR.output),
      cost('Input $', 'cost_input', COLOR.input),
      cost('Cache Read $', 'cost_cache_read', COLOR.cache_read),
      cost('Cache Write $', 'cost_cache_write', COLOR.cache_write),
      cost('Output $', 'cost_output', COLOR.output),
      cost('Request $', 'cost_request', COLOR.request),
    ],
  }
}

export function pie(title: string, data: { name: string; value: number }[], isCost: boolean): ChartOption {
  return {
    title: { text: title, left: 'center', textStyle: { fontSize: 13, color: '#6B7280' } },
    tooltip: { trigger: 'item', formatter: '{b}: {d}%' },
    series: [{ name: title, type: 'pie', radius: ['30%', '60%'], label: { formatter: isCost ? '{b}\n${c}' : '{b}\n{c}', fontSize: 10 }, data }],
  }
}

export function usageTrend(points: TrendPoint[]): ChartOption {
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { top: 40, bottom: 40, left: 60, right: 60 },
    xAxis: { type: 'category', data: points.map((p) => p.period) },
    yAxis: [{ type: 'value', name: 'Invocations' }, { type: 'value', name: 'Cost ($)' }],
    series: [
      { name: 'Invocations', type: 'bar', itemStyle: { color: COLOR.input }, data: points.map((p) => p.invocations) },
      { name: 'Cost ($)', type: 'line', yAxisIndex: 1, smooth: true, itemStyle: { color: '#F97316' }, data: points.map((p) => round4(p.cost_usd)) },
    ],
  }
}

export function minAvgMaxBar(labels: string[], rows: ModelRow[], unit: string, keys: [keyof ModelRow, keyof ModelRow, keyof ModelRow]): ChartOption {
  const [min, avg, max] = keys
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { top: 40, bottom: 90, left: 70, right: 20 },
    xAxis: axisLabels(labels),
    yAxis: { type: 'value', name: unit },
    series: [
      { name: 'Min', type: 'bar', itemStyle: { color: MIN_MAX.min }, data: rows.map((r) => r[min]) },
      { name: 'Avg', type: 'bar', itemStyle: { color: MIN_MAX.avg }, data: rows.map((r) => r[avg]) },
      { name: 'Max', type: 'bar', itemStyle: { color: MIN_MAX.max }, data: rows.map((r) => r[max]) },
    ],
  }
}

export function latencyTrend(points: TrendPoint[]): ChartOption {
  const line = (name: string, key: keyof TrendPoint, color: string, dashed = false) =>
    ({ name, type: 'line', itemStyle: { color }, lineStyle: dashed ? { type: 'dashed' } : undefined, data: points.map((p) => p[key]) })
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { top: 40, bottom: 40, left: 70, right: 20 },
    xAxis: { type: 'category', data: points.map((p) => p.period) },
    yAxis: { type: 'value', name: 'ms' },
    series: [line('Min', 'min_latency_ms', MIN_MAX.min), line('Avg', 'avg_latency_ms', MIN_MAX.avg, true), line('Max', 'max_latency_ms', MIN_MAX.max)],
  }
}

export function ttftTrend(points: TtftPoint[], tz: string, withDate: boolean): ChartOption {
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { top: 40, bottom: 40, left: 70, right: 20 },
    xAxis: { type: 'category', data: points.map((p) => formatInTz(p.timestamp, tz, withDate)) },
    yAxis: { type: 'value', name: 'ms' },
    series: [
      { name: 'Avg TTFT', type: 'line', smooth: true, itemStyle: { color: '#6366F1' }, data: points.map((p) => p.ttft_avg) },
      { name: 'P99 TTFT', type: 'line', itemStyle: { color: '#A78BFA' }, lineStyle: { type: 'dashed' }, data: points.map((p) => p.ttft_p99) },
    ],
  }
}
