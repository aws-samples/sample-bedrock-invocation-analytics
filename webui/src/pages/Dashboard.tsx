import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Alert from '@cloudscape-design/components/alert'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Container from '@cloudscape-design/components/container'
import ContentLayout from '@cloudscape-design/components/content-layout'
import DateRangePicker, { type DateRangePickerProps } from '@cloudscape-design/components/date-range-picker'
import Grid from '@cloudscape-design/components/grid'
import Header from '@cloudscape-design/components/header'
import Select, { type SelectProps } from '@cloudscape-design/components/select'
import SpaceBetween from '@cloudscape-design/components/space-between'
import Spinner from '@cloudscape-design/components/spinner'
import { api, type RangeParams, type Status } from '../api'
import EChart from '../components/EChart'
import UsageBreakdown from '../components/UsageBreakdown'
import { latencyTrend, minAvgMaxBar, ttftTrend, usageTrend } from '../components/charts'
import {
  DEFAULT_RANGE, LOCAL_TZ, RELATIVE_OPTIONS, formatCost, formatInTz, formatMs, formatNumber, resolveRange, shortModel, toZonedIso, validateRange,
} from '../format'

const TZ_OPTIONS: SelectProps.Option[] = LOCAL_TZ === 'UTC'
  ? [{ value: 'UTC', label: 'UTC' }]
  : [{ value: LOCAL_TZ, label: `${LOCAL_TZ} (local)` }, { value: 'UTC', label: 'UTC' }]
const REFRESH_OPTIONS: SelectProps.Option[] = [
  { value: '0', label: 'Auto refresh off' }, { value: '60', label: 'Every 1 min' }, { value: '300', label: 'Every 5 min' },
]
const TOTAL: SelectProps.Option = { value: 'TOTAL', label: 'All' }

/** Current time, advanced every `seconds` (0 = only on demand). Relative ranges are resolved against it. */
function useNow(seconds: number) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    if (!seconds) return
    const id = setInterval(() => setNow(new Date()), seconds * 1000)
    return () => clearInterval(id)
  }, [seconds])
  return [now, () => setNow(new Date())] as const
}

export default function Dashboard({ status }: { status?: Status }) {
  const [account, setAccount] = useState<SelectProps.Option | null>(null)
  const [range, setRange] = useState<DateRangePickerProps.Value>(DEFAULT_RANGE)
  const [tz, setTz] = useState<SelectProps.Option>(TZ_OPTIONS[0])
  const [refresh, setRefresh] = useState<SelectProps.Option>(REFRESH_OPTIONS[0])
  const [now, refreshNow] = useNow(Number(refresh.value))
  const [usageModel, setUsageModel] = useState<SelectProps.Option>(TOTAL)
  const [usageCaller, setUsageCaller] = useState<SelectProps.Option>(TOTAL)
  const [latencyModel, setLatencyModel] = useState<SelectProps.Option>(TOTAL)
  const [ttftModel, setTtftModel] = useState<SelectProps.Option | null>(null)

  const accounts = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const accountOptions = (accounts.data ?? []).map((a) => ({
    value: a.key, label: a.name ? `${a.account_id} (${a.name})` : a.account_id, description: a.region,
  }))
  const selectedAccount = account ?? accountOptions[0] ?? null
  const accountKey = selectedAccount?.value
  const tzName = tz.value!

  const params: RangeParams | null = useMemo(() => {
    if (!accountKey) return null
    const [start, end] = resolveRange(range, tzName, now)
    return { account: accountKey, start: toZonedIso(start, tzName), end: toZonedIso(end, tzName), tz: tzName }
  }, [accountKey, range, tzName, now])

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
  const cards = summary && [
    ['Total invocations', formatNumber(summary.invocations)],
    ['Input tokens', formatNumber(summary.input_tokens)],
    ['Output tokens', formatNumber(summary.output_tokens)],
    ['Cache tokens', formatNumber(summary.cache_read_tokens + summary.cache_write_tokens)],
    ['Estimated cost', formatCost(summary.cost_usd)],
    ['Avg latency', formatMs(summary.avg_latency_ms)],
    ['Avg TPOT', `${summary.avg_tpot.toFixed(2)}ms`],
  ]

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={status?.data_up_to
            ? `Data up to ${formatInTz(status.data_up_to, tzName, true)}. Events reach the dashboard a few minutes after the call.`
            : 'Waiting for the first cost aggregation run.'}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Select selectedOption={refresh} onChange={(e) => setRefresh(e.detail.selectedOption)} options={REFRESH_OPTIONS} ariaLabel="Auto refresh" />
              <Button iconName="refresh" ariaLabel="Refresh" loading={dashboard.isFetching} onClick={refreshNow} />
            </SpaceBetween>
          }
        >
          Usage dashboard
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Container>
          <ColumnLayout columns={3}>
            <Select selectedOption={selectedAccount} onChange={(e) => setAccount(e.detail.selectedOption)} options={accountOptions}
              statusType={accounts.isLoading ? 'loading' : 'finished'} placeholder="Account" ariaLabel="Account" />
            <DateRangePicker value={range} onChange={(e) => e.detail.value && setRange(e.detail.value)} relativeOptions={RELATIVE_OPTIONS}
              isValidRange={validateRange} dateOnly rangeSelectorMode="default" placeholder="Time range" i18nStrings={{ ariaLabel: 'Time range' }} />
            <Select selectedOption={tz} onChange={(e) => setTz(e.detail.selectedOption)} options={TZ_OPTIONS} ariaLabel="Time zone" />
          </ColumnLayout>
        </Container>

        {status && status.unpriced_models.length > 0 && (
          <Alert type="warning" header={`${status.unpriced_models.length} model(s) counted at $0`}>
            No price is set for {status.unpriced_models.join(', ')}. Add one on the Pricing page, then run scripts/rebuild_costs.sh.
          </Alert>
        )}
        {accounts.isSuccess && accountOptions.length === 0 && <Alert type="info">No usage recorded yet.</Alert>}
        {dashboard.error && <Alert type="error">{dashboard.error.message}</Alert>}
        {!data && dashboard.isLoading && <Box textAlign="center"><Spinner size="large" /></Box>}

        {data && cards && (
          <>
            <Container>
              <ColumnLayout columns={4} variant="text-grid" minColumnWidth={140}>
                {cards.map(([title, value]) => (
                  <div key={title}>
                    <Box variant="awsui-key-label">{title}</Box>
                    <Box variant="awsui-value-large">{value}</Box>
                  </div>
                ))}
              </ColumnLayout>
            </Container>

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

            <Grid gridDefinition={[{ colspan: { default: 12, m: 6 } }, { colspan: { default: 12, m: 6 } }]}>
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
          </>
        )}
      </SpaceBetween>
    </ContentLayout>
  )
}
