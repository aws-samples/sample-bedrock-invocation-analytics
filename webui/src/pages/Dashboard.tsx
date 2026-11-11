import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router'
import Alert from '@cloudscape-design/components/alert'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import ContentLayout from '@cloudscape-design/components/content-layout'
import Grid from '@cloudscape-design/components/grid'
import Header from '@cloudscape-design/components/header'
import Icon, { type IconProps } from '@cloudscape-design/components/icon'
import Link from '@cloudscape-design/components/link'
import Select, { type SelectProps } from '@cloudscape-design/components/select'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Spinner from '@cloudscape-design/components/spinner'
import { api, MODEL_KINDS, REPRICE_ACTIVE, type Status, type TrendPoint, type UnpricedModel } from '../api'
import EChart from '../components/EChart'
import UsageBreakdown from '../components/UsageBreakdown'
import { latencyTrend, minAvgMaxBar, ttftTrend, usageTrend } from '../components/charts'
import { useFilters } from '../filters-context'
import { formatCost, formatInTz, formatMs, formatNumber, shortModel } from '../format'

const TOTAL: SelectProps.Option = { value: 'TOTAL', label: 'All' }

type CardKind = 'tokens' | 'requests' | 'cost' | 'speed'

// Dismissing the unpriced banner hides it until the page is reloaded, or until the set of unpriced models changes.
// Module scope, so it survives navigating to the Pricing page and back.
let dismissedUnpriced = ''

export default function Dashboard({ status }: { status?: Status }) {
  const { params, tz: tzName, noData, refresh } = useFilters()
  const navigate = useNavigate()
  const unpricedKey = (status?.unpriced_models ?? []).map((m) => m.model).join('\n')
  const [, setDismissed] = useState(dismissedUnpriced)  // re-render after dismissing
  const [usageModel, setUsageModel] = useState<SelectProps.Option>(TOTAL)
  const [usageCaller, setUsageCaller] = useState<SelectProps.Option>(TOTAL)
  const [latencyModel, setLatencyModel] = useState<SelectProps.Option>(TOTAL)
  const [ttftModel, setTtftModel] = useState<SelectProps.Option | null>(null)

  const dashboard = useQuery({ queryKey: ['dashboard', params], queryFn: () => api.dashboard(params!), enabled: !!params, placeholderData: (prev) => prev })
  const data = dashboard.data
  // After a filter change the previous range stays on screen, under a loading mask, until the new one arrives.
  const stale = dashboard.isPlaceholderData
  const models = data?.models ?? []
  const modelOptions = [TOTAL, ...models.map((m) => ({ value: `MODEL#${m.model}`, label: m.model }))]
  const callerOptions = [TOTAL, ...(data?.callers ?? []).map((c) => ({ value: `CALLER#${c.caller}`, label: c.caller }))]

  // "All" comes with the dashboard response, so it changes together with it; a model or caller needs its own request.
  const usageDim = usageModel.value !== 'TOTAL' ? usageModel.value! : usageCaller.value!
  const trendQuery = (dim: string) => ({
    queryKey: ['trend', params, dim],
    queryFn: () => api.trend(params!, dim),
    enabled: !!params && dim !== 'TOTAL',
    placeholderData: (prev: TrendPoint[] | undefined) => prev,
  })
  const usageTrendQuery = useQuery(trendQuery(usageDim))
  const latencyTrendQuery = useQuery(trendQuery(latencyModel.value!))
  const trendOf = (dim: string, q: typeof usageTrendQuery) =>
    dim === 'TOTAL' ? { points: data?.trend_total, loading: stale } : { points: q.data, loading: q.isPlaceholderData }
  const usageTrendData = trendOf(usageDim, usageTrendQuery)
  const latencyTrendData = trendOf(latencyModel.value!, latencyTrendQuery)

  const ttftSelected = ttftModel ?? (models[0] ? { value: models[0].model, label: models[0].model } : null)
  const ttft = useQuery({
    queryKey: ['ttft', ttftSelected?.value, params?.start, params?.end],
    queryFn: () => api.ttft(ttftSelected!.value!, params!.start, params!.end),
    enabled: !!params && !!ttftSelected,
    placeholderData: (prev) => prev,
  })
  const spanDays = params ? (Date.parse(params.end) - Date.parse(params.start)) / 864e5 : 0
  const usageTrendOption = useMemo(() => usageTrendData.points && usageTrend(usageTrendData.points), [usageTrendData.points])
  const latencyTrendOption = useMemo(() => latencyTrendData.points && latencyTrend(latencyTrendData.points), [latencyTrendData.points])
  const ttftOption = useMemo(() => ttft.data && ttftTrend(ttft.data, tzName, spanDays > 1), [ttft.data, tzName, spanDays])

  const charts = useMemo(() => {
    if (!data) return null
    const top = data.models.slice(0, 15)
    const labels = top.map((m) => shortModel(m.model))
    return {
      latency: minAvgMaxBar(labels, top, 'ms', ['min_latency_ms', 'avg_latency_ms', 'max_latency_ms']),
      tpot: minAvgMaxBar(labels, top, 'ms/token', ['tpot_min', 'tpot_avg', 'tpot_max']),
    }
  }, [data])

  const summary = data?.summary
  // Token counts first, then requests, cost and speed, so each group fills one row of four.
  // Blue = sent to the model / written to the cache, green = returned by the model / read from the cache.
  // The last field tints the card by what it measures.
  const cards: [string, string, IconProps.Name | keyof typeof CUSTOM_ICONS, IconProps.Variant, CardKind][] | undefined = summary && [
    ['Input tokens', formatNumber(summary.input_tokens), 'upload', 'link', 'tokens'],
    ['Output tokens', formatNumber(summary.output_tokens), 'download', 'success', 'tokens'],
    ['Cache write tokens', formatNumber(summary.cache_write_tokens), 'cache-write', 'link', 'tokens'],
    ['Cache read tokens', formatNumber(summary.cache_read_tokens), 'cache-read', 'success', 'tokens'],
    ['Total invocations', formatNumber(summary.invocations), 'send', 'link', 'requests'],
    ['Estimated cost', formatCost(summary.cost_usd), 'cost', 'warning', 'cost'],
    ['Avg latency', formatMs(summary.avg_latency_ms), 'status-pending', 'subtle', 'speed'],
    ['Avg TPOT', `${summary.avg_tpot.toFixed(2)}ms`, 'speed', 'subtle', 'speed'],
  ]
  const rangeText = params && `${formatInTz(params.start, tzName, true)} → ${formatInTz(params.end, tzName, true)}`

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={[
            status?.data_up_to ? `Data up to ${formatInTz(status.data_up_to, tzName, true)}` : 'Waiting for the first cost aggregation run',
            rangeText,
          ].filter(Boolean).join(' · ')}
          actions={<Button iconName="refresh" loading={dashboard.isFetching} onClick={refresh}>Refresh</Button>}
        >
          Usage dashboard
        </Header>
      }
    >
      <SpaceBetween size="l">
        {status && status.unpriced_models.length > 0 && unpricedKey !== dismissedUnpriced && (
          <Alert type="warning" header={`${status.unpriced_models.length} model(s) counted at $0`} dismissible
            onDismiss={() => { dismissedUnpriced = unpricedKey; setDismissed(unpricedKey) }}>
            <SpaceBetween size="xxs">
              <span>No price applies to these models. {status.user.is_admin ? 'Set one on the Pricing page, then reprice usage.'
                : `Ask a member of ${status.admin_group} to set one.`}</span>
              <ul className="unpriced-list">
                {status.unpriced_models.map((m) => (
                  <li key={m.model}>
                    <UnpricedLine m={m} tz={tzName} />{' '}
                    {status.user.is_admin && <Link href={`/pricing?model=${encodeURIComponent(m.model)}`}
                      onFollow={(e) => { e.preventDefault(); navigate(`/pricing?model=${encodeURIComponent(m.model)}`) }}>Set price</Link>}
                  </li>
                ))}
              </ul>
              {status.unpriced_models.some((m) => m.kind === 'application-inference-profile') && (
                <Box variant="small">
                  An application inference profile is billed as the model it was created from. Find it with{' '}
                  <code>aws bedrock get-inference-profile --inference-profile-identifier &lt;id&gt;</code>; for a deleted profile, look up its
                  CreateInferenceProfile event in CloudTrail.
                </Box>
              )}
            </SpaceBetween>
          </Alert>
        )}
        {status?.reprice && !status.reprice.dry_run && REPRICE_ACTIVE.includes(status.reprice.status) && (
          <Alert type="info" header="Repricing past usage">
            {status.reprice.months_total ? `${status.reprice.months_done ?? 0} of ${status.reprice.months_total} months done. ` : ''}
            Figures change as each month finishes.
          </Alert>
        )}
        {noData && <Alert type="info">No usage recorded yet.</Alert>}
        {dashboard.error && <Alert type="error">{dashboard.error.message}</Alert>}
        {!data && dashboard.isLoading && <Box textAlign="center"><Spinner size="large" /></Box>}

        {data && cards && (
          <SpaceBetween size="l">
            <div className={stale ? 'summary-cards is-loading' : 'summary-cards'} aria-busy={stale}>
              {cards.map(([title, value, icon, variant, kind]) => (
                <Container key={title} fitHeight disableContentPaddings>
                  <div className={`summary-card summary-card--${kind}`}>
                    <div className="summary-card-label">
                      {icon in CUSTOM_ICONS ? <Icon svg={CUSTOM_ICONS[icon as keyof typeof CUSTOM_ICONS]} variant={variant} />
                        : <Icon name={icon as IconProps.Name} variant={variant} />}
                      <Box variant="awsui-key-label">{title}</Box>
                    </div>
                    <Box fontSize="heading-xl" fontWeight="bold" padding={{ top: 'xs' }}>
                      <span style={{ whiteSpace: 'nowrap' }}>{value}</span>
                    </Box>
                  </div>
                </Container>
              ))}
            </div>

            <Container header={
              <Header variant="h2" actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Select selectedOption={usageModel} options={modelOptions} filteringType="auto" ariaLabel="Model"
                    onChange={(e) => { setUsageModel(e.detail.selectedOption); setUsageCaller(TOTAL) }} />
                  <Select selectedOption={usageCaller} options={callerOptions} filteringType="auto" ariaLabel="Caller"
                    onChange={(e) => { setUsageCaller(e.detail.selectedOption); setUsageModel(TOTAL) }} />
                </SpaceBetween>
              }>Usage trend</Header>
            }>
              {usageTrendOption ? <EChart option={usageTrendOption} loading={usageTrendData.loading} ariaLabel="Usage trend" /> : <Spinner />}
            </Container>

            <UsageBreakdown title="Token usage and cost by model" rows={models} nameKey="model" nameHeader="Model" loading={stale} />
            <UsageBreakdown title="Token usage and cost by caller" rows={data.callers} nameKey="caller" nameHeader="Caller" loading={stale} />

            {/* Rows pair a per-model snapshot with its trend: latency, then TPOT with TTFT. */}
            <Grid gridDefinition={Array.from({ length: 4 }, () => ({ colspan: { default: 12, m: 6 } }))}>
              <Container header={<Header variant="h2">Latency by model</Header>}>
                <EChart option={charts!.latency} loading={stale} ariaLabel="Latency by model" />
              </Container>
              <Container header={
                <Header variant="h2" actions={
                  <Select selectedOption={latencyModel} options={modelOptions} filteringType="auto" ariaLabel="Model"
                    onChange={(e) => setLatencyModel(e.detail.selectedOption)} />
                }>Latency trend</Header>
              }>
                {latencyTrendOption ? <EChart option={latencyTrendOption} loading={latencyTrendData.loading} ariaLabel="Latency trend" /> : <Spinner />}
              </Container>
              <Container header={<Header variant="h2" description="Latency divided by output tokens">TPOT by model (approx.)</Header>}>
                <EChart option={charts!.tpot} loading={stale} ariaLabel="TPOT by model" />
              </Container>
              <Container header={
                <Header variant="h2" description="From CloudWatch AWS/Bedrock TimeToFirstToken" actions={
                  <Select selectedOption={ttftSelected} options={modelOptions.slice(1).map((o) => ({ value: o.label, label: o.label }))}
                    filteringType="auto" ariaLabel="Model" placeholder="Model" onChange={(e) => setTtftModel(e.detail.selectedOption)} />
                }>TTFT trend</Header>
              }>
                {ttftOption ? <EChart option={ttftOption} loading={ttft.isPlaceholderData} ariaLabel="TTFT trend" /> : <Spinner />}
              </Container>
            </Grid>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </ContentLayout>
  )
}

function UnpricedLine({ m, tz }: { m: UnpricedModel; tz: string }) {
  const where = m.accounts.map((a) => `${a.name} ${a.region}`).join(', ')
  const day = (iso?: string) => (iso ? formatInTz(iso, tz, true).slice(0, 10) : '')
  const span = m.first_seen && day(m.first_seen) !== day(m.last_seen) ? `${day(m.first_seen)} – ${day(m.last_seen)}` : day(m.first_seen)
  return (
    <span>
      {m.kind ? `${MODEL_KINDS[m.kind] ?? m.kind} ` : ''}<b>{m.id}</b>
      {[where, span].filter(Boolean).map((t) => ` · ${t}`)}
    </span>
  )
}

// Icons Cloudscape lacks, drawn in its 16px stroke style: cost, speed (TPOT), and cache hit / cache write on a database.
const DB = 'M2 3.5c0 1.1 2.2 2 5 2s5-.9 5-2-2.2-2-5-2-5 .9-5 2Zm0 0v8c0 1.1 2.2 2 5 2M2 7.5c0 1.1 2.2 2 5 2M12 3.5V8'
const CUSTOM_ICONS = {
  cost: (
    <svg viewBox="0 0 16 16" focusable="false" aria-hidden="true">
      <circle cx="8" cy="8" r="7" />
      <path d="M10.4 5.6C10 5 9.1 4.6 8 4.6c-1.4 0-2.4.7-2.4 1.7s1 1.4 2.4 1.7 2.4.7 2.4 1.7-1 1.7-2.4 1.7c-1.1 0-2-.4-2.4-1M8 3.2v1.4M8 11.4v1.4" />
    </svg>
  ),
  speed: (
    <svg viewBox="0 0 16 16" focusable="false" aria-hidden="true">
      <path d="M2.3 12.5a6.5 6.5 0 1 1 11.4 0M8 10.5l3-3.5" />
      <circle cx="8" cy="11" r="1" />
    </svg>
  ),
  'cache-read': (
    <svg viewBox="0 0 16 16" focusable="false" aria-hidden="true">
      <path d={DB} />
      <path d="M13 9.5l-2.5 3h3l-2.5 3" />
    </svg>
  ),
  'cache-write': (
    <svg viewBox="0 0 16 16" focusable="false" aria-hidden="true">
      <path d={DB} />
      <path d="M12.5 10v5M10 12.5h5" />
    </svg>
  ),
}
