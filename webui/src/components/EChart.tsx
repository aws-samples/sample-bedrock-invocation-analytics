import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { AriaComponent, GridComponent, LegendComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, TitleComponent, AriaComponent, CanvasRenderer])

export type ChartOption = echarts.EChartsCoreOption

interface Props {
  option: ChartOption
  height?: number
  ariaLabel: string
  loading?: boolean  // keeps the current chart under a loading mask while newer data is on its way
}

/** Thin ECharts wrapper: one instance per element, resized with its container, options replaced on change. */
export default function EChart({ option, height = 360, ariaLabel, loading = false }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const instance = echarts.init(ref.current)
    chart.current = instance
    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(ref.current)
    return () => {
      observer.disconnect()
      instance.dispose()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    chart.current?.setOption({ aria: { enabled: true }, ...option }, { notMerge: true })
  }, [option])

  useEffect(() => {
    if (loading) chart.current?.showLoading('default', { text: '', color: '#0972d3', maskColor: 'rgba(255, 255, 255, 0.6)' })
    else chart.current?.hideLoading()
  }, [loading])

  return <div ref={ref} role="img" aria-label={ariaLabel} aria-busy={loading} style={{ width: '100%', height }} />
}
