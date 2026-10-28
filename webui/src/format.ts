import type { DateRangePickerProps } from '@cloudscape-design/components/date-range-picker'

export const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

export const formatNumber = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}K` : String(n)
export const formatCost = (usd: number) => `$${usd.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
export const formatMs = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms.toFixed(2)}ms`)
export const round4 = (n: number) => Math.round(n * 10_000) / 10_000
export const shortModel = (model: string) => model.replace(/^(global|us|eu|apac)\./, '').replace(/^(anthropic|meta)\./, '').slice(0, 28)

/** Wall-clock parts of `date` in time zone `tz`. */
function zonedParts(date: Date, tz: string) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(date)
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value)
  return { year: get('year'), month: get('month'), day: get('day'), hour: get('hour'), minute: get('minute'), second: get('second') }
}

/** UTC offset of `tz` at `date`, in minutes. */
function offsetMinutes(date: Date, tz: string) {
  const p = zonedParts(date, tz)
  return (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(date.getTime() / 1000) * 1000) / 60_000
}

/** ISO 8601 string with the numeric offset of `tz`, e.g. 2026-10-28T08:00:00+08:00 (what the API expects). */
export function toZonedIso(date: Date, tz: string) {
  const p = zonedParts(date, tz)
  const off = offsetMinutes(date, tz)
  const pad = (n: number, w = 2) => String(Math.abs(n)).padStart(w, '0')
  return `${p.year}-${pad(p.month)}-${pad(p.day)}T${pad(p.hour)}:${pad(p.minute)}:${pad(p.second)}${off < 0 ? '-' : '+'}${pad(Math.trunc(off / 60))}:${pad(off % 60)}`
}

/** Instant for wall-clock `y-m-d h:mi` in `tz`. */
function fromZoned(y: number, m: number, d: number, h: number, mi: number, tz: string) {
  const guess = new Date(Date.UTC(y, m - 1, d, h, mi))
  return new Date(guess.getTime() - offsetMinutes(guess, tz) * 60_000)
}

export function formatInTz(iso: string, tz: string, withDate = false) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, hourCycle: 'h23', hour: '2-digit', minute: '2-digit', ...(withDate ? { year: 'numeric', month: '2-digit', day: '2-digit' } : {}),
  }).format(new Date(iso))
}

export const RELATIVE_OPTIONS: DateRangePickerProps.RelativeOption[] = [
  { key: 'last-24h', amount: 24, unit: 'hour', type: 'relative' },
  { key: 'last-7d', amount: 7, unit: 'day', type: 'relative' },
  { key: 'last-30d', amount: 30, unit: 'day', type: 'relative' },
  { key: 'last-90d', amount: 90, unit: 'day', type: 'relative' },
]

export const DEFAULT_RANGE: DateRangePickerProps.Value = RELATIVE_OPTIONS[1]

const UNIT_MS: Record<string, number> = { second: 1e3, minute: 6e4, hour: 36e5, day: 864e5, week: 6048e5 }

/** Resolve a picker value to [start, end) instants. Absolute dates are whole days in `tz`, end day inclusive. */
export function resolveRange(value: DateRangePickerProps.Value, tz: string, now = new Date()): [Date, Date] {
  if (value.type === 'relative') {
    if (value.unit === 'month' || value.unit === 'year') {
      const p = zonedParts(now, tz)
      const months = value.unit === 'year' ? value.amount * 12 : value.amount
      return [fromZoned(p.year, p.month - months, p.day, p.hour, p.minute, tz), now]
    }
    return [new Date(now.getTime() - value.amount * UNIT_MS[value.unit]), now]
  }
  const [sy, sm, sd] = value.startDate.slice(0, 10).split('-').map(Number)
  const [ey, em, ed] = value.endDate.slice(0, 10).split('-').map(Number)
  const end = fromZoned(ey, em, ed + 1, 0, 0, tz)
  return [fromZoned(sy, sm, sd, 0, 0, tz), end > now ? now : end]
}

export function validateRange(value: DateRangePickerProps.Value | null): DateRangePickerProps.ValidationResult {
  if (!value) return { valid: false, errorMessage: 'Select a range' }
  if (value.type === 'absolute') {
    if (!value.startDate || !value.endDate) return { valid: false, errorMessage: 'Select start and end dates' }
    if (value.startDate > value.endDate) return { valid: false, errorMessage: 'Start must be before end' }
    const days = (Date.parse(value.endDate.slice(0, 10)) - Date.parse(value.startDate.slice(0, 10))) / 864e5
    if (days > 399) return { valid: false, errorMessage: 'Range must not exceed 400 days' }
  }
  return { valid: true }
}
