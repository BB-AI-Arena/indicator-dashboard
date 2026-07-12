import { useEffect, useMemo, useRef, useState } from 'react'
import { createChart } from 'lightweight-charts'
import { ResponsiveContainer, LineChart, Line, BarChart, Bar, XAxis, YAxis, Tooltip } from 'recharts'

export default function TickerChart({ indicatorData, tradeMarkers = [], priceLevels = [] }) {
  const chartRef = useRef(null)
  const chartContainerRef = useRef(null)
  const [chartError, setChartError] = useState('')

  const finiteNumber = (v) => Number.isFinite(v)

  const sanitizedCandles = useMemo(() => {
    const rows = Array.isArray(indicatorData?.candles) ? indicatorData.candles : []
    const map = new Map()
    rows.forEach((r) => {
      const t = Number(r?.time)
      const o = Number(r?.open)
      const h = Number(r?.high)
      const l = Number(r?.low)
      const c = Number(r?.close)
      if (!finiteNumber(t) || !finiteNumber(o) || !finiteNumber(h) || !finiteNumber(l) || !finiteNumber(c)) return
      map.set(t, { time: t, open: o, high: h, low: l, close: c, volume: Number(r?.volume) || 0 })
    })
    return [...map.values()].sort((a, b) => a.time - b.time)
  }, [indicatorData])

  const buildLineData = (key) => {
    const rows = Array.isArray(indicatorData?.indicators) ? indicatorData.indicators : []
    const map = new Map()
    rows.forEach((r) => {
      const t = Number(r?.time)
      const v = Number(r?.[key])
      if (!finiteNumber(t) || !finiteNumber(v)) return
      map.set(t, { time: t, value: v })
    })
    return [...map.values()].sort((a, b) => a.time - b.time)
  }

  const lineDefinitions = useMemo(() => {
    const supplied = Array.isArray(indicatorData?.line_indicators) ? indicatorData.line_indicators : []
    if (supplied.length) return supplied
    return [
      { key: 'ema_fast', label: 'EMA 9', color: '#16c784' },
      { key: 'ema_slow', label: 'EMA 21', color: '#f0b90b' },
      { key: 'ema_trend', label: 'EMA 50', color: '#ef4444' },
      { key: 'ema_200', label: 'EMA 200', color: '#a78bfa' },
      { key: 'vwap', label: 'VWAP', color: '#60a5fa' },
    ]
  }, [indicatorData])

  useEffect(() => {
    if (!chartContainerRef.current) return
    if (!sanitizedCandles.length) {
      chartRef.current?.remove()
      chartRef.current = null
      setChartError('No valid candle data available for chart rendering.')
      return
    }
    setChartError('')

    try {
      chartRef.current?.remove()
      const chart = createChart(chartContainerRef.current, {
        layout: { background: { color: '#111b2e' }, textColor: '#94a3b8' },
        grid: { vertLines: { color: '#1f2b45' }, horzLines: { color: '#1f2b45' } },
        width: chartContainerRef.current.clientWidth,
        height: 320,
        rightPriceScale: { borderColor: '#324056' },
        timeScale: { borderColor: '#324056' },
      })
      const candles = chart.addCandlestickSeries()
      const lineSeries = lineDefinitions.map((def) => chart.addLineSeries({ color: def.color || '#60a5fa', lineWidth: 1 }))

      candles.setData(sanitizedCandles)
      lineSeries.forEach((series, idx) => {
        const key = lineDefinitions[idx]?.key
        if (!key) return
        series.setData(buildLineData(key))
      })

      const markers = Array.isArray(tradeMarkers) ? tradeMarkers.filter((marker) => Number.isFinite(Number(marker?.time))) : []
      if (markers.length) {
        candles.setMarkers(markers.map((marker) => ({
          time: Number(marker.time),
          position: marker.position || 'belowBar',
          color: marker.color || '#60a5fa',
          shape: marker.shape || 'circle',
          text: marker.text || '',
        })))
      }

      const levels = Array.isArray(priceLevels) ? priceLevels : []
      levels.forEach((level) => {
        const price = Number(level?.price)
        if (!Number.isFinite(price)) return
        candles.createPriceLine({
          price,
          color: level.color || '#94a3b8',
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: level.label || '',
        })
      })

      chart.timeScale().fitContent()
      chartRef.current = chart
    } catch (e) {
      chartRef.current?.remove()
      chartRef.current = null
      setChartError(`Chart render error: ${e?.message || 'unknown error'}`)
    }

    return () => {
      chartRef.current?.remove()
      chartRef.current = null
    }
  }, [indicatorData, sanitizedCandles, lineDefinitions, tradeMarkers, priceLevels])

  useEffect(() => {
    if (!chartContainerRef.current || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(() => {
      if (chartRef.current && chartContainerRef.current) {
        chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth })
      }
    })
    observer.observe(chartContainerRef.current)
    return () => observer.disconnect()
  }, [])

  const rechartsData = (indicatorData?.indicators || []).map((d, idx) => ({
    idx,
    rsi: d.rsi,
    macd: d.macd_line,
    signal: d.macd_signal,
    hist: d.macd_hist,
    vol: indicatorData?.candles?.[idx]?.volume,
  }))

  return (
    <div className="space-y-4">
      {chartError && <div className="card p-3 text-sm text-amber-300">{chartError}</div>}
      <div className="card p-2"><div ref={chartContainerRef} className="w-full" /></div>
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="card h-52 p-3"><h4 className="mb-2 text-sm">RSI</h4><ResponsiveContainer width="100%" height="85%"><LineChart data={rechartsData}><XAxis dataKey="idx" hide /><YAxis domain={[0, 100]} /><Tooltip /><Line dot={false} dataKey="rsi" stroke="#f0b90b" /></LineChart></ResponsiveContainer></div>
        <div className="card h-52 p-3"><h4 className="mb-2 text-sm">MACD</h4><ResponsiveContainer width="100%" height="85%"><LineChart data={rechartsData}><XAxis dataKey="idx" hide /><YAxis /><Tooltip /><Line dot={false} dataKey="macd" stroke="#16c784" /><Line dot={false} dataKey="signal" stroke="#ef4444" /></LineChart></ResponsiveContainer></div>
        <div className="card h-52 p-3"><h4 className="mb-2 text-sm">Volume</h4><ResponsiveContainer width="100%" height="85%"><BarChart data={rechartsData}><XAxis dataKey="idx" hide /><YAxis /><Tooltip /><Bar dataKey="vol" fill="#60a5fa" /></BarChart></ResponsiveContainer></div>
      </div>
    </div>
  )
}
