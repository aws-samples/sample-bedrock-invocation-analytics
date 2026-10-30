import { useMemo } from 'react'
import { useCollection } from '@cloudscape-design/collection-hooks'
import Box from '@cloudscape-design/components/box'
import ColumnLayout from '@cloudscape-design/components/column-layout'
import Container from '@cloudscape-design/components/container'
import Header from '@cloudscape-design/components/header'
import Table, { type TableProps } from '@cloudscape-design/components/table'
import Tabs from '@cloudscape-design/components/tabs'
import { isNotTokenBilled, type UsageRow } from '../api'
import { formatCost, formatNumber, round4 } from '../format'
import EChart from './EChart'
import { pie, usageCostBar } from './charts'

const TOP_BARS = 15
const TOP_SLICES = 10

interface Props<T extends UsageRow> {
  title: string
  rows: T[]
  nameKey: keyof T & string
  nameHeader: string
}

export default function UsageBreakdown<T extends UsageRow>({ title, rows, nameKey, nameHeader }: Props<T>) {
  const label = (row: T) => String(row[nameKey])
  const barOption = useMemo(() => {
    const top = rows.slice(0, TOP_BARS)
    return usageCostBar(top.map((r) => String(r[nameKey])), top)
  }, [rows, nameKey])
  const pies = useMemo(() => {
    const slice = (key: keyof UsageRow, isCost: boolean) =>
      rows.slice(0, TOP_SLICES).map((r) => ({ name: String(r[nameKey]).slice(0, 22), value: isCost ? round4(r[key]) : r[key] }))
    return [
      pie('Input Tokens', slice('input_tokens', false), false),
      pie('Output Tokens', slice('output_tokens', false), false),
      pie('Cost', slice('cost_usd', true), true),
    ]
  }, [rows, nameKey])

  // Imported and provisioned models are billed per model unit and time; a $0 here would read as free.
  const money = (key: keyof UsageRow) => (r: T) => (isNotTokenBilled(label(r))
    ? <Box variant="small" color="text-status-inactive">not estimated</Box> : formatCost(r[key]))
  const columns: TableProps.ColumnDefinition<T>[] = [
    { id: 'name', header: nameHeader, cell: label, sortingComparator: (a, b) => label(a).localeCompare(label(b)), isRowHeader: true },
    { id: 'invocations', header: 'Calls', cell: (r) => r.invocations.toLocaleString(), sortingField: 'invocations' },
    { id: 'input_tokens', header: 'Input tokens', cell: (r) => formatNumber(r.input_tokens), sortingField: 'input_tokens' },
    { id: 'output_tokens', header: 'Output tokens', cell: (r) => formatNumber(r.output_tokens), sortingField: 'output_tokens' },
    { id: 'cost_cache_read', header: 'Cache read', cell: money('cost_cache_read'), sortingField: 'cost_cache_read' },
    { id: 'cost_cache_write', header: 'Cache write', cell: money('cost_cache_write'), sortingField: 'cost_cache_write' },
    { id: 'cost_input', header: 'Input', cell: money('cost_input'), sortingField: 'cost_input' },
    { id: 'cost_output', header: 'Output', cell: money('cost_output'), sortingField: 'cost_output' },
    { id: 'cost_request', header: 'Per request', cell: money('cost_request'), sortingField: 'cost_request' },
    { id: 'cost_usd', header: 'Total', cell: money('cost_usd'), sortingField: 'cost_usd' },
  ]
  const { items, collectionProps } = useCollection(rows, {
    sorting: { defaultState: { sortingColumn: columns[columns.length - 1], isDescending: true } },
  })

  return (
    <Container header={<Header variant="h2" counter={`(${rows.length})`}>{title}</Header>} disableContentPaddings={false}>
      <Tabs
        tabs={[
          { id: 'chart', label: 'Chart', content: <EChart option={barOption} height={400} ariaLabel={`${title}: tokens and cost`} /> },
          {
            id: 'pie', label: 'Pie', content: (
              <ColumnLayout columns={3}>
                {pies.map((option, i) => <EChart key={i} option={option} height={320} ariaLabel={`${title}: share`} />)}
              </ColumnLayout>
            ),
          },
          {
            id: 'table', label: 'Table', content: (
              <Table {...collectionProps} items={items} columnDefinitions={columns} variant="embedded" stickyHeader
                empty={<Box textAlign="center" color="inherit">No usage in this range</Box>} />
            ),
          },
        ]}
      />
    </Container>
  )
}
