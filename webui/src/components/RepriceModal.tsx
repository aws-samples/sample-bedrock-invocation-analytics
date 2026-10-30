import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Alert from '@cloudscape-design/components/alert'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import DatePicker from '@cloudscape-design/components/date-picker'
import FormField from '@cloudscape-design/components/form-field'
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs'
import Modal from '@cloudscape-design/components/modal'
import ProgressBar from '@cloudscape-design/components/progress-bar'
import RadioGroup from '@cloudscape-design/components/radio-group'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Table from '@cloudscape-design/components/table'
import { api, ApiError, REPRICE_ACTIVE, type RepriceDiff, type RepriceJob } from '../api'
import { formatCost, formatNumber } from '../format'

const utcDay = (d: Date) => d.toISOString().slice(0, 10)
const localDay = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const monthStart = (day: string) => `${day.slice(0, 7)}-01`
const previousMonth = (day: string) => {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCMonth(d.getUTCMonth() - 1)
  return utcDay(d).slice(0, 7)
}
const usd = (micro: number) => formatCost(micro / 1e6)
const exact = (n: number) => n.toLocaleString('en-US')  // a drop of one call must stay visible

/** Mount it when opening (it keeps no state across openings). */
export default function RepriceModal({ initialStart, onDismiss }: { initialStart?: string; onDismiss: () => void }) {
  const queryClient = useQueryClient()
  const job = useQuery({
    queryKey: ['reprice'],
    queryFn: api.reprice,
    refetchInterval: (q) => (q.state.data && REPRICE_ACTIVE.includes(q.state.data.status) ? 3000 : false),
  })
  const accounts = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const [today] = useState(() => utcDay(new Date()))
  const [scope, setScope] = useState<'from' | 'all'>('from')
  const [start, setStart] = useState(() => initialStart ?? utcDay(new Date(Date.now() - 30 * 86_400_000)))

  const current = job.data
  const active = job.isLoading || (!!current && REPRICE_ACTIVE.includes(current.status))
  const run = useMutation({
    mutationFn: (dryRun: boolean) => api.startReprice(scope === 'all' ? 'all' : start, dryRun),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['reprice'] }),
  })
  const cancel = useMutation({
    mutationFn: (id: string) => api.cancelReprice(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['reprice'] }),
  })

  const names = useMemo(() => Object.fromEntries((accounts.data ?? []).map((a) => [a.key, a.name])), [accounts.data])
  const conflict = run.error instanceof ApiError && run.error.status === 409
  const validStart = scope === 'all' || (/^\d{4}-\d{2}-\d{2}$/.test(start) && start <= today)

  return (
    <Modal visible onDismiss={onDismiss} size="large" header="Reprice usage"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>Close</Button>
            <Button disabled={active || !validStart} loading={run.isPending && run.variables} onClick={() => run.mutate(true)}>Preview</Button>
            <Button variant="primary" disabled={active || !validStart} loading={run.isPending && !run.variables}
              onClick={() => run.mutate(false)}>Reprice</Button>
          </SpaceBetween>
        </Box>
      }>
      <SpaceBetween size="l">
        <Box variant="p">
          Recomputes cost and usage from the event log with the current prices and replaces the stored figures.
          Preview shows the changes without writing them.
        </Box>
        <FormField label="Range" description={scope === 'all' ? 'Every day the event log covers, through now.'
          : `From ${monthStart(start)} (whole UTC months) through now.`}>
          <SpaceBetween size="xs">
            <RadioGroup value={scope} onChange={(e) => setScope(e.detail.value as 'from' | 'all')}
              items={[{ value: 'from', label: 'From a date' }, { value: 'all', label: 'All history' }]} />
            {scope === 'from' && (
              <DatePicker value={start} onChange={(e) => setStart(e.detail.value)} placeholder="YYYY/MM/DD"
                isDateEnabled={(d) => localDay(d) <= today} />
            )}
          </SpaceBetween>
        </FormField>
        {run.error && !conflict && <Alert type="error">{run.error.message}</Alert>}
        {cancel.error && <Alert type="error">{cancel.error.message}</Alert>}
        {conflict && <Alert type="info">A reprice job is already running. Its progress is shown below.</Alert>}
        {current && <JobView job={current} names={names} onCancel={() => cancel.mutate(current.job_id)} cancelling={cancel.isPending} />}
      </SpaceBetween>
    </Modal>
  )
}

function JobView({ job, names, onCancel, cancelling }: { job: RepriceJob; names: Record<string, string>; onCancel: () => void; cancelling: boolean }) {
  const kind = job.dry_run ? 'Preview' : 'Reprice'
  const range = job.start === 'all' ? 'all history' : `from ${monthStart(job.start)}`
  const done = job.months_done ?? 0
  const total = Math.max(job.months_total ?? 0, done)
  const updated = job.updated_at ? `Last update ${new Date(job.updated_at).toLocaleTimeString()}` : undefined

  if (job.status === 'queued' || job.status === 'running' || job.status === 'cancelling') {
    const note = [updated, job.stalls ? `retrying (${job.stalls} of 3)` : undefined].filter(Boolean).join(' · ')
    return (
      <SpaceBetween size="xs">
        <ProgressBar
          label={`${kind} ${range}`}
          value={total ? Math.round((done / total) * 100) : 0}
          description={job.status === 'queued' ? 'Waiting to start (within a few minutes)'
            : job.status === 'cancelling' ? 'Cancelling after the current month'
            : `${done} of ${total} months${job.dry_run ? '' : ', then today'}`}
          additionalInfo={note || undefined}
        />
        {job.status !== 'cancelling' && <Button loading={cancelling} onClick={onCancel}>Cancel {kind.toLowerCase()}</Button>}
      </SpaceBetween>
    )
  }
  return (
    <SpaceBetween size="m">
      {job.status === 'done' && (
        <Alert type="success" header={`${kind} ${range} finished${job.finished_at ? ` at ${new Date(job.finished_at).toLocaleString()}` : ''}`}>
          {job.dry_run ? 'Nothing was written. Run Reprice to apply these changes.' : 'Stored figures now use the current prices.'}
        </Alert>
      )}
      {job.status === 'failed' && <Alert type="error" header={`${kind} ${range} failed`}>{job.error ?? 'Unknown error'}</Alert>}
      {job.status === 'cancelled' && (
        <Alert type="info" header={`${kind} ${range} cancelled`}>
          {job.dry_run ? 'Nothing was written.'
            : done && job.next ? `Repriced through ${previousMonth(job.next)}; later months keep their previous figures.`
            : 'No month was repriced.'}
        </Alert>
      )}
      {job.totals && (
        <KeyValuePairs columns={4} items={[
          { label: 'Days recomputed', value: formatNumber(job.totals.days) },
          { label: 'Days changed', value: formatNumber(job.totals.days_changed) },
          { label: 'Months changed', value: formatNumber(job.totals.months_changed) },
          { label: 'Invocation drops', value: job.totals.drops ? <StatusIndicator type="warning">{job.totals.drops}</StatusIndicator> : '0' },
        ]} />
      )}
      {job.diffs && job.diffs.length > 0 && <DiffTable diffs={job.diffs} names={names} />}
    </SpaceBetween>
  )
}

function DiffTable({ diffs, names }: { diffs: RepriceDiff[]; names: Record<string, string> }) {
  return (
    <Table variant="embedded" items={diffs} stickyHeader
      header={<Box variant="h4">Changes ({diffs.length}{diffs.length >= 300 ? ', largest shown' : ''})</Box>}
      columnDefinitions={[
        { id: 'account', header: 'Account', cell: (d) => names[d.pk] ?? d.pk },
        { id: 'period', header: 'Period (UTC)', cell: (d) => d.period },
        { id: 'invocations', header: 'Invocations', cell: (d) => (
          d.drop ? <StatusIndicator type="warning">{`${exact(d.invocations_before)} → ${exact(d.invocations_after)}`}</StatusIndicator>
            : `${exact(d.invocations_before)} → ${exact(d.invocations_after)}`) },
        { id: 'cost', header: 'Cost', cell: (d) => `${usd(d.cost_before)} → ${usd(d.cost_after)}` },
        { id: 'delta', header: 'Δ cost', cell: (d) => `${d.cost_after >= d.cost_before ? '+' : '−'}${usd(Math.abs(d.cost_after - d.cost_before))}` },
      ]} />
  )
}
