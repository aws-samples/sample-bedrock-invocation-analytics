import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@cloudscape-design/components/box'
import DateRangePicker, { type DateRangePickerProps } from '@cloudscape-design/components/date-range-picker'
import FormField from '@cloudscape-design/components/form-field'
import Header from '@cloudscape-design/components/header'
import Select, { type SelectProps } from '@cloudscape-design/components/select'
import SpaceBetween from '@cloudscape-design/components/space-between'
import { api } from './api'
import { FiltersContext, type Filters } from './filters-context'
import { DEFAULT_RANGE, LOCAL_TZ, RELATIVE_OPTIONS, formatRelativeRange, resolveRange, toZonedIso, validateRange } from './format'

const TZ_OPTIONS: SelectProps.Option[] = LOCAL_TZ === 'UTC'
  ? [{ value: 'UTC', label: 'UTC' }]
  : [{ value: LOCAL_TZ, label: `${LOCAL_TZ} (local)` }, { value: 'UTC', label: 'UTC' }]
const REFRESH_OPTIONS: SelectProps.Option[] = [
  { value: '0', label: 'Off' }, { value: '60', label: 'Every 1 minute' }, { value: '300', label: 'Every 5 minutes' },
]

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

/** Dashboard filters (account, region, range, time zone, auto refresh). Rendered in the collapsible side panel. */
export function FiltersProvider({ children }: { children: ReactNode }) {
  const [accountId, setAccountId] = useState<string | null>(null)
  const [region, setRegion] = useState<string | null>(null)
  const [range, setRange] = useState<DateRangePickerProps.Value>(DEFAULT_RANGE)
  const [tz, setTz] = useState<SelectProps.Option>(TZ_OPTIONS[0])
  const [refresh, setRefresh] = useState<SelectProps.Option>(REFRESH_OPTIONS[0])
  const [now, refreshNow] = useNow(Number(refresh.value))

  const accounts = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const rows = accounts.data ?? []
  const accountOptions = [...new Map(rows.map((a) => [a.account_id, {
    value: a.account_id, label: a.name ? `${a.account_id} (${a.name})` : a.account_id,
  }])).values()]
  const selectedAccount = accountOptions.find((o) => o.value === accountId) ?? accountOptions[0] ?? null
  const regionOptions = rows.filter((a) => a.account_id === selectedAccount?.value).map((a) => ({ value: a.region, label: a.region }))
  const selectedRegion = regionOptions.find((o) => o.value === region) ?? regionOptions[0] ?? null
  const accountKey = selectedAccount && selectedRegion ? `${selectedAccount.value}#${selectedRegion.value}` : undefined
  const tzName = tz.value!

  const params = useMemo(() => {
    if (!accountKey) return null
    const [start, end] = resolveRange(range, tzName, now)
    return { account: accountKey, start: toZonedIso(start, tzName), end: toZonedIso(end, tzName), tz: tzName }
  }, [accountKey, range, tzName, now])

  const panel = (
    <Box padding="m">
      <SpaceBetween size="l">
        <Header variant="h2">Filters</Header>
        <FormField label="Account">
          <Select selectedOption={selectedAccount} options={accountOptions} statusType={accounts.isLoading ? 'loading' : 'finished'}
            placeholder="No accounts yet" onChange={(e) => { setAccountId(e.detail.selectedOption.value!); setRegion(null) }} />
        </FormField>
        <FormField label="Region">
          <Select selectedOption={selectedRegion} options={regionOptions} placeholder="—" onChange={(e) => setRegion(e.detail.selectedOption.value!)} />
        </FormField>
        <FormField label="Time range">
          <DateRangePicker value={range} onChange={(e) => e.detail.value && setRange(e.detail.value)} relativeOptions={RELATIVE_OPTIONS}
            isValidRange={validateRange} dateOnly rangeSelectorMode="default" placeholder="Time range" expandToViewport
            i18nStrings={{ ariaLabel: 'Time range', formatRelativeRange }} />
        </FormField>
        <FormField label="Time zone">
          <Select selectedOption={tz} options={TZ_OPTIONS} onChange={(e) => setTz(e.detail.selectedOption)} />
        </FormField>
        <FormField label="Auto refresh">
          <Select selectedOption={refresh} options={REFRESH_OPTIONS} onChange={(e) => setRefresh(e.detail.selectedOption)} />
        </FormField>
      </SpaceBetween>
    </Box>
  )

  const value: Filters = { params, tz: tzName, noData: accounts.isSuccess && rows.length === 0, refresh: refreshNow, panel }
  return <FiltersContext.Provider value={value}>{children}</FiltersContext.Provider>
}
