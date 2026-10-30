import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCollection } from '@cloudscape-design/collection-hooks'
import Alert from '@cloudscape-design/components/alert'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import ContentLayout from '@cloudscape-design/components/content-layout'
import Form from '@cloudscape-design/components/form'
import FormField from '@cloudscape-design/components/form-field'
import Header from '@cloudscape-design/components/header'
import Input from '@cloudscape-design/components/input'
import Modal from '@cloudscape-design/components/modal'
import Pagination from '@cloudscape-design/components/pagination'
import SpaceBetween from '@cloudscape-design/components/space-between'
import StatusIndicator from '@cloudscape-design/components/status-indicator'
import Table, { type TableProps } from '@cloudscape-design/components/table'
import TextFilter from '@cloudscape-design/components/text-filter'
import { api, PRICE_FIELDS, type PriceField, type PricingRecord, type Status } from '../api'
import RepriceModal from '../components/RepriceModal'

const LABELS: Record<PriceField, string> = {
  input_per_1k: 'Input $/1K tokens',
  output_per_1k: 'Output $/1K tokens',
  cache_read_per_1k: 'Cache read $/1K tokens',
  cache_write_per_1k: 'Cache write (5 min) $/1K tokens',
  cache_write_1h_per_1k: 'Cache write (1 h) $/1K tokens',
  per_request: 'Per request $',
}
const DESCRIPTIONS: Partial<Record<PriceField, string>> = {
  cache_write_1h_per_1k: '0 = use the 5-minute price',
  per_request: 'Rerank and image generation models',
}
const price = (n: number, digits = 6) => (n ? n.toFixed(digits) : '—')
const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z')

interface Draft {
  model: string
  effectiveDate: string
  originalDate?: string
  prices: Record<PriceField, string>
}

function emptyDraft(model = '', record?: PricingRecord): Draft {
  return {
    model,
    effectiveDate: record?.effective_date ?? nowIso(),
    originalDate: record?.effective_date,
    prices: Object.fromEntries(PRICE_FIELDS.map((f) => [f, String(record?.[f] ?? 0)])) as Record<PriceField, string>,
  }
}

export default function Pricing({ status }: { status?: Status }) {
  const isAdmin = !!status?.user.is_admin
  const queryClient = useQueryClient()
  const pricing = useQuery({ queryKey: ['pricing'], queryFn: api.pricing })
  const [historyModel, setHistoryModel] = useState<string | null>(null)
  const history = useQuery({ queryKey: ['pricing', historyModel], queryFn: () => api.pricingHistory(historyModel!), enabled: !!historyModel })
  const [draft, setDraft] = useState<Draft | null>(null)
  const [toDelete, setToDelete] = useState<PricingRecord | null>(null)
  const [reprice, setReprice] = useState<{ start?: string } | null>(null)
  const [saved, setSaved] = useState<{ model: string; day: string } | null>(null)

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['pricing'] })
  const save = useMutation({
    mutationFn: async (d: Draft) => {
      const prices = Object.fromEntries(PRICE_FIELDS.map((f) => [f, Number(d.prices[f])])) as Record<PriceField, number>
      await api.savePricing(d.model, prices, d.effectiveDate)
      if (d.originalDate && d.originalDate !== d.effectiveDate) await api.deletePricing(d.model, d.originalDate)
    },
    onSuccess: (_r, d) => {
      setDraft(null)
      invalidate()
      // Costs change from the earlier of the old and new effective dates (UTC); future prices need no reprice.
      const from = Math.min(Date.parse(d.effectiveDate), d.originalDate ? Date.parse(d.originalDate) : Infinity)
      setSaved(from <= Date.now() ? { model: d.model, day: new Date(from).toISOString().slice(0, 10) } : null)
    },
  })
  const remove = useMutation({
    mutationFn: (r: PricingRecord) => api.deletePricing(r.model_id, r.effective_date),
    onSuccess: () => { setToDelete(null); invalidate() },
  })

  const columns: TableProps.ColumnDefinition<PricingRecord>[] = [
    { id: 'model_id', header: 'Model ID', isRowHeader: true, sortingField: 'model_id',
      cell: (r) => <Button variant="inline-link" onClick={() => setHistoryModel(r.model_id)}>{r.model_id}</Button> },
    { id: 'input', header: 'Input $/1K', cell: (r) => price(r.input_per_1k), sortingField: 'input_per_1k' },
    { id: 'output', header: 'Output $/1K', cell: (r) => price(r.output_per_1k), sortingField: 'output_per_1k' },
    { id: 'cache_read', header: 'Cache read $/1K', cell: (r) => price(r.cache_read_per_1k), sortingField: 'cache_read_per_1k' },
    { id: 'cache_write', header: 'Cache write $/1K', cell: (r) => price(r.cache_write_per_1k), sortingField: 'cache_write_per_1k' },
    { id: 'per_request', header: '$/request', cell: (r) => price(r.per_request, 4), sortingField: 'per_request' },
    { id: 'effective_date', header: 'Effective', cell: (r) => r.effective_date, sortingField: 'effective_date' },
    { id: 'source', header: 'Source', cell: (r) => r.source, sortingField: 'source' },
  ]
  const { items, collectionProps, filterProps, paginationProps, filteredItemsCount } = useCollection(pricing.data ?? [], {
    filtering: { filteringFunction: (r, text) => r.model_id.toLowerCase().includes(text.toLowerCase()) },
    pagination: { pageSize: 25 },
    sorting: { defaultState: { sortingColumn: columns[0] } },
  })

  const sync = status?.pricing_sync
  return (
    <ContentLayout header={
      <Header variant="h1" description={sync?.synced_at
        ? `Last LiteLLM sync ${sync.synced_at}: ${sync.models_updated ?? 0} updated, ${sync.models_added ?? 0} added, ${sync.models_skipped ?? 0} unchanged`
        : 'Prices sync weekly from LiteLLM. Manual prices take effect from their effective date.'}>
        Pricing
      </Header>
    }>
      <SpaceBetween size="l">
        {saved && (
          <Alert type="success" dismissible onDismiss={() => setSaved(null)}
            action={<Button onClick={() => setReprice({ start: saved.day })}>Reprice usage</Button>}>
            Saved the {saved.model} price. Past usage keeps its previous cost until you reprice it from {saved.day}.
          </Alert>
        )}
        {!isAdmin && <Alert type="info">You have read-only access. Members of the {status?.admin_group ?? 'admin'} group can edit prices.</Alert>}
        <Table
          {...collectionProps}
          items={items}
          columnDefinitions={columns}
          loading={pricing.isLoading}
          loadingText="Loading prices"
          stickyHeader
          header={<Header counter={`(${pricing.data?.length ?? 0})`}
            actions={isAdmin && (
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setReprice({})}>Reprice usage</Button>
                <Button onClick={() => setDraft(emptyDraft())}>Add model price</Button>
              </SpaceBetween>
            )}>Models</Header>}
          filter={<TextFilter {...filterProps} filteringPlaceholder="Find model" countText={`${filteredItemsCount ?? 0} matches`} />}
          pagination={<Pagination {...paginationProps} />}
          empty={<Box textAlign="center">No prices</Box>}
        />
      </SpaceBetween>

      <Modal visible={!!historyModel} onDismiss={() => setHistoryModel(null)} size="max" header={`Price history: ${historyModel ?? ''}`}
        footer={isAdmin && <Box float="right"><Button onClick={() => setDraft(emptyDraft(historyModel!))}>Add price</Button></Box>}>
        <Table
          variant="embedded"
          items={history.data ?? []}
          loading={history.isLoading}
          columnDefinitions={[
            { id: 'effective_date', header: 'Effective', cell: (r) => r.effective_date },
            ...PRICE_FIELDS.map((f) => ({ id: f, header: LABELS[f], cell: (r: PricingRecord) => price(r[f], f === 'per_request' ? 4 : 6) })),
            { id: 'source', header: 'Source', cell: (r) => (r.updated_by ? `${r.source} (${r.updated_by})` : r.source) },
            ...(isAdmin ? [{
              id: 'actions', header: 'Actions', cell: (r: PricingRecord) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="inline-icon" iconName="edit" ariaLabel="Edit" onClick={() => setDraft(emptyDraft(r.model_id, r))} />
                  <Button variant="inline-icon" iconName="remove" ariaLabel="Delete" onClick={() => setToDelete(r)} />
                </SpaceBetween>
              ),
            }] : []),
          ]}
        />
      </Modal>

      {draft && (
        <Modal visible onDismiss={() => setDraft(null)} header={draft.originalDate ? 'Edit price' : 'Add price'}>
          <form onSubmit={(e) => { e.preventDefault(); save.mutate(draft) }}>
            <Form actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" formAction="none" onClick={() => setDraft(null)}>Cancel</Button>
                <Button variant="primary" loading={save.isPending} disabled={!draft.model}>Save</Button>
              </SpaceBetween>
            } errorText={save.error?.message}>
              <SpaceBetween size="s">
                <FormField label="Model ID">
                  <Input value={draft.model} readOnly={!!draft.originalDate} onChange={(e) => setDraft({ ...draft, model: e.detail.value })} />
                </FormField>
                {PRICE_FIELDS.map((f) => (
                  <FormField key={f} label={LABELS[f]} description={DESCRIPTIONS[f]}>
                    <Input type="number" inputMode="decimal" value={draft.prices[f]}
                      onChange={(e) => setDraft({ ...draft, prices: { ...draft.prices, [f]: e.detail.value } })} />
                  </FormField>
                ))}
                <FormField label="Effective date (UTC, ISO 8601)">
                  <Input value={draft.effectiveDate} onChange={(e) => setDraft({ ...draft, effectiveDate: e.detail.value })} />
                </FormField>
                <StatusIndicator type="info">Past usage keeps its previous cost until you reprice it.</StatusIndicator>
              </SpaceBetween>
            </Form>
          </form>
        </Modal>
      )}

      <Modal visible={!!toDelete} onDismiss={() => setToDelete(null)} header="Delete price"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setToDelete(null)}>Cancel</Button>
              <Button variant="primary" loading={remove.isPending} onClick={() => toDelete && remove.mutate(toDelete)}>Delete</Button>
            </SpaceBetween>
          </Box>
        }>
        {toDelete && `Delete the ${toDelete.model_id} price effective ${toDelete.effective_date}?`}
        {remove.error && <Alert type="error">{remove.error.message}</Alert>}
      </Modal>
      {isAdmin && reprice && <RepriceModal initialStart={reprice.start} onDismiss={() => setReprice(null)} />}
    </ContentLayout>
  )
}
