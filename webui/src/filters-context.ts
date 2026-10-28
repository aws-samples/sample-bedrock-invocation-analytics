import { createContext, useContext, type ReactNode } from 'react'
import type { RangeParams } from './api'

export interface Filters {
  params: RangeParams | null
  tz: string
  noData: boolean
  refresh: () => void
  panel: ReactNode
}

export const FiltersContext = createContext<Filters | null>(null)

export function useFilters(): Filters {
  const value = useContext(FiltersContext)
  if (!value) throw new Error('useFilters must be used inside FiltersProvider')
  return value
}
