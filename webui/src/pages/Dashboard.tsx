import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@cloudscape-design/components/alert'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import ContentLayout from '@cloudscape-design/components/content-layout'
import Grid from '@cloudscape-design/components/grid'
import Header from '@cloudscape-design/components/header'
import Icon, { type IconProps } from '@cloudscape-design/components/icon'
import Select, { type SelectProps } from '@cloudscape-design/components/select'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Spinner from '@cloudscape-design/components/spinner'
import { api, type Status } from '../api'
import EChart from '../components/EChart'
import UsageBreakdown from '../components/UsageBreakdown'
import { latencyTrend, minAvgMaxBar, ttftTrend, usageTrend } from '../components/charts'
import { useFilters } from '../filters-context'
import { formatCost, formatInTz, formatMs, formatNumber, shortModel } from '../format'

const TOTAL: SelectProps.Option = { value: 'TOTAL', label: 'All' }

export default function Dashboard({ status }: { status?: Status }) {
  const { params, tz: tzName, noData, refresh } = useFilters()
  const [usageModel, setUsageModel] = useState<SelectProps.Option>(TOTAL)
  const [usageCaller, setUsageCaller] = useState<SelectProps.Option>(TOTAL)
  const [latencyModel, setLatencyModel] = useState<SelectProps.Option>(TOTAL)
  const [ttftModel, setTtftModel] = useState<SelectProps.Option | null>(null)

  const dashboard = useQuery({ queryKey: ['dashboard', params], queryFn: () => api.dashboard(params!), enabled: !!params, placeholderData: (prev) => prev })
  const data = dashboard.data
  const models = data?.models ?? []
  const modelOptions = [TOTAL, ...models.map((m) => ({ value: `MODEL#${m.model}`, label: m.model }))]
  const callerOptions = [TOTAL, ...(data?.callers ?? []).map((c) => ({ value: `CALLER#${c.caller}`, label: c.caller }))]

  const usageDim = usageModel.value !== 'TOTAL' ? usageModel.value! : usageCaller.value!
  const trendQuery = (dim: string) => ({
    queryKey: ['trend', params, dim],
    queryFn: () => (dim === 'TOTAL' ? Promise.resolve(data!.trend_total) : api.trend(params!, dim)),
    enabled: !!params && !!data,
    placeholderData: (prev: unknown) => prev,
  })
  const usageTrendData = useQuery({ ...trendQuery(usageDim), placeholderData: undefined })
  const latencyTrendData = useQuery({ ...trendQuery(latencyModel.value!), placeholderData: undefined })

  const ttftSelected = ttftModel ?? (models[0] ? { value: models[0].model, label: models[0].model } : null)
  const ttft = useQuery({
    queryKey: ['ttft', ttftSelected?.value, params?.start, params?.end],
    queryFn: () => api.ttft(ttftSelected!.value!, params!.start, params!.end),
    enabled: !!params && !!ttftSelected,
  })
  const spanDays = params ? (Date.parse(params.end) - Date.parse(params.start)) / 864e5 : 0
  const usageTrendOption = useMemo(() => usageTrendData.data && usageTrend(usageTrendData.data), [usageTrendData.data])
  const latencyTrendOption = useMemo(() => latencyTrendData.data && latencyTrend(latencyTrendData.data), [latencyTrendData.data])
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
  const cards: [string, string, IconProps.Name, IconProps.Variant][] | undefined = summary && [
    ['Total invocations', formatNumber(summary.invocations), 'call', 'link'],
    ['Input tokens', formatNumber(summary.input_tokens), 'upload', 'success'],
    ['Output tokens', formatNumber(summary.output_tokens), 'download', 'warning'],
    ['Cache tokens', formatNumber(summary.cache_read_tokens + summary.cache_write_tokens), 'redo', 'link'],
    ['Estimated cost', formatCost(summary.cost_usd), 'ticket', 'error'],
    ['Avg latency', formatMs(summary.avg_latency_ms), 'status-pending', 'subtle'],
    ['Avg TPOT', `${summary.avg_tpot.toFixed(2)}ms`, 'status-in-progress', 'subtle'],
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
        {status && status.unpriced_models.length > 0 && (
          <Alert type="warning" header={`${status.unpriced_models.length} model(s) counted at $0`}>
            No price is set for {status.unpriced_models.join(', ')}. Add one on the Pricing page, then run scripts/rebuild_costs.sh.
          </Alert>
        )}
        {noData && <Alert type="info">No usage recorded yet.</Alert>}
        {dashboard.error && <Alert type="error">{dashboard.error.message}</Alert>}
        {!data && dashboard.isLoading && <Box textAlign="center"><Spinner size="large" /></Box>}

        {data && cards && (
          <SpaceBetween size="l">
            <div className="summary-cards">
              {cards.map(([title, value, icon, variant]) => (
                <Container key={title} fitHeight>
                  <div className="summary-card">
                    <div className="summary-card-label">
                      <Icon name={icon} variant={variant} />
                      <Box variant="awsui-key-label">{title}</Box>
                    </div>
                    <Box fontSize="heading-xl" fontWeight="bold" padding={{ top: 'xs' }}>
                      <span style={{ whiteSpace: 'nowrap' }}>{value}</span>
                    </Box>
                  </div>
                </Container>
              ))}
            </div>

            <UsageBreakdown title="Token usage and cost by model" rows={models} nameKey="model" nameHeader="Model" />
            <UsageBreakdown title="Token usage and cost by caller" rows={data.callers} nameKey="caller" nameHeader="Caller" />

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
              {usageTrendOption ? <EChart option={usageTrendOption} ariaLabel="Usage trend" /> : <Spinner />}
            </Container>

            <Grid gridDefinition={Array.from({ length: 4 }, () => ({ colspan: { default: 12, m: 6 } }))}>
              <Container header={<Header variant="h2">Latency by model</Header>}>
                <EChart option={charts!.latency} ariaLabel="Latency by model" />
              </Container>
              <Container header={<Header variant="h2" description="Latency divided by output tokens">TPOT by model (approx.)</Header>}>
                <EChart option={charts!.tpot} ariaLabel="TPOT by model" />
              </Container>
              <Container header={
                <Header variant="h2" actions={
                  <Select selectedOption={latencyModel} options={modelOptions} filteringType="auto" ariaLabel="Model"
                    onChange={(e) => setLatencyModel(e.detail.selectedOption)} />
                }>Latency trend</Header>
              }>
                {latencyTrendOption ? <EChart option={latencyTrendOption} ariaLabel="Latency trend" /> : <Spinner />}
              </Container>
              <Container header={
                <Header variant="h2" description="From CloudWatch AWS/Bedrock TimeToFirstToken" actions={
                  <Select selectedOption={ttftSelected} options={modelOptions.slice(1).map((o) => ({ value: o.label, label: o.label }))}
                    filteringType="auto" ariaLabel="Model" placeholder="Model" onChange={(e) => setTtftModel(e.detail.selectedOption)} />
                }>TTFT trend</Header>
              }>
                {ttftOption ? <EChart option={ttftOption} ariaLabel="TTFT trend" /> : <Spinner />}
              </Container>
            </Grid>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </ContentLayout>
  )
}
