import { useEffect, useMemo, useRef, useState } from 'react'
import { createChart } from 'lightweight-charts'
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip } from 'recharts'

function latestValue(indicatorData, key) {
  const latest = indicatorData?.latest || (indicatorData?.indicators || []).at(-1) || {}
  const value = Number(latest?.[key])
  return Number.isFinite(value) ? value : null
}

function formatValue(value, digits = 2) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '-'
}

export default function TickerChart({ indicatorData, tradeMarkers = [], priceLevels = [] }) {
  const chartRef = useRef(null)
  const chartContainerRef = useRef(null)
  const [chartError, setChartError] = useState('')
  const [advancedOverlays, setAdvancedOverlays] = useState(false)

  const finiteNumber = (value) => Number.isFinite(value)

  const sanitizedCandles = useMemo(() => {
    const rows = Array.isArray(indicatorData?.candles) ? indicatorData.candles : []
    const map = new Map()
    rows.forEach((row) => {
      const time = Number(row?.time)
      const open = Number(row?.open)
      const high = Number(row?.high)
      const low = Number(row?.low)
      const close = Number(row?.close)
      if (![time, open, high, low, close].every(finiteNumber)) return
      map.set(time, { time, open, high, low, close, volume: Number(row?.volume) || 0 })
    })
    return [...map.values()].sort((a, b) => a.time - b.time)
  }, [indicatorData])

  const suppliedLineDefinitions = useMemo(() => {
    const supplied = Array.isArray(indicatorData?.line_indicators) ? indicatorData.line_indicators : []
    return supplied.length
      ? supplied
      : [
          { key: 'ema_fast', label: 'EMA 9', color: '#16c784' },
          { key: 'ema_slow', label: 'EMA 21', color: '#f0b90b' },
          { key: 'ema_trend', label: 'EMA 50', color: '#ef4444' },
          { key: 'ema_200', label: 'EMA 200', color: '#a78bfa' },
          { key: 'vwap', label: 'VWAP', color: '#60a5fa' },
        ]
  }, [indicatorData])

  const lineDefinitions = useMemo(() => {
    if (advancedOverlays) return suppliedLineDefinitions
    const vwap = suppliedLineDefinitions.find((definition) => definition.key === 'vwap')
    return vwap ? [vwap] : [{ key: 'vwap', label: 'VWAP', color: '#60a5fa' }]
  }, [advancedOverlays, suppliedLineDefinitions])

  const buildLineData = (key) => {
    const rows = Array.isArray(indicatorData?.indicators) ? indicatorData.indicators : []
    const map = new Map()
    rows.forEach((row) => {
      const time = Number(row?.time)
      const value = Number(row?.[key])
      if (!finiteNumber(time) || !finiteNumber(value)) return
      map.set(time, { time, value })
    })
    return [...map.values()].sort((a, b) => a.time - b.time)
  }

  const visiblePriceLevels = useMemo(() => {
    const levels = Array.isArray(priceLevels) ? priceLevels : []
    if (advancedOverlays) return levels
    const current = Number(indicatorData?.latest?.close || sanitizedCandles.at(-1)?.close)
    const atr = Number(indicatorData?.latest?.atr)
    const distance = Number.isFinite(current) ? Math.max(Number.isFinite(atr) ? atr * 2 : 0, current * 0.025) : null
    return levels.filter((level) => {
      const price = Number(level?.price)
      if (!Number.isFinite(price)) return false
      const label = String(level?.label || '').toLowerCase()
      const isFib = /\d+(\.\d+)?%/.test(label)
      if (!isFib) return true
      const hasConfluence = label.includes('strong') || label.includes('major')
      const isActive = distance !== null && Math.abs(price - current) <= distance
      return hasConfluence || isActive
    })
  }, [advancedOverlays, indicatorData, priceLevels, sanitizedCandles])

  useEffect(() => {
    if (!chartContainerRef.current) return undefined
    if (!sanitizedCandles.length) {
      chartRef.current?.remove()
      chartRef.current = null
      setChartError('No valid candle data available for chart rendering.')
      return undefined
    }
    setChartError('')

    try {
      chartRef.current?.remove()
      const chart = createChart(chartContainerRef.current, {
        layout: { background: { color: '#111b2e' }, textColor: '#94a3b8' },
        grid: { vertLines: { color: '#1f2b45' }, horzLines: { color: '#1f2b45' } },
        width: chartContainerRef.current.clientWidth,
        height: advancedOverlays ? 380 : 340,
        rightPriceScale: { borderColor: '#324056' },
        timeScale: { borderColor: '#324056', rightOffset: 4 },
      })
      const candles = chart.addCandlestickSeries({
        upColor: '#16c784',
        downColor: '#ff4d5a',
        borderVisible: false,
        wickUpColor: '#16c784',
        wickDownColor: '#ff4d5a',
      })
      const volume = chart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
        color: 'rgba(56, 189, 248, 0.38)',
        base: 0,
      })
      chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } })
      const lineSeries = lineDefinitions.map((definition) => chart.addLineSeries({ color: definition.color || '#60a5fa', lineWidth: 2 }))

      candles.setData(sanitizedCandles)
      volume.setData(sanitizedCandles.map((row) => ({
        time: row.time,
        value: row.volume,
        color: row.close >= row.open ? 'rgba(22, 199, 132, 0.48)' : 'rgba(255, 77, 90, 0.48)',
      })))
      lineSeries.forEach((series, index) => {
        const key = lineDefinitions[index]?.key
        if (key) series.setData(buildLineData(key))
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

      visiblePriceLevels.forEach((level) => {
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
    } catch (error) {
      chartRef.current?.remove()
      chartRef.current = null
      setChartError(`Chart render error: ${error?.message || 'unknown error'}`)
    }

    return () => {
      chartRef.current?.remove()
      chartRef.current = null
    }
  }, [advancedOverlays, indicatorData, lineDefinitions, sanitizedCandles, tradeMarkers, visiblePriceLevels])

  useEffect(() => {
    if (!chartContainerRef.current || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(() => {
      if (chartRef.current && chartContainerRef.current) chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth })
    })
    observer.observe(chartContainerRef.current)
    return () => observer.disconnect()
  }, [])

  const momentumData = (indicatorData?.indicators || []).map((row, index) => ({
    index,
    rsi: row.rsi,
    macd: row.macd_line,
    signal: row.macd_signal,
    volume: sanitizedCandles[index]?.volume,
  }))

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-700 bg-slate-950/40 px-3 py-2 text-xs">
        <span className="font-semibold uppercase tracking-wide text-slate-300">Primary view: VWAP, volume, key levels</span>
        <button
          type="button"
          className="rounded border border-slate-600 bg-slate-900 px-3 py-1.5 font-semibold text-slate-200 hover:border-cyan-400 hover:text-white"
          aria-expanded={advancedOverlays}
          onClick={() => setAdvancedOverlays((value) => !value)}
        >
          {advancedOverlays ? 'Hide advanced overlays' : 'Advanced overlays'}
        </button>
      </div>
      {chartError && <div className="card p-3 text-sm text-amber-300">{chartError}</div>}
      <div className="card p-2"><div ref={chartContainerRef} className="w-full" /></div>
      <div className="grid gap-2 text-xs text-slate-400 sm:grid-cols-4">
        <span>VWAP: <strong className="text-slate-200">{formatValue(latestValue(indicatorData, 'vwap'))}</strong></span>
        <span>EMA 21: <strong className="text-slate-200">{advancedOverlays ? formatValue(latestValue(indicatorData, 'ema_slow')) : 'advanced'}</strong></span>
        <span>RSI: <strong className="text-slate-200">{advancedOverlays ? formatValue(latestValue(indicatorData, 'rsi'), 1) : 'advanced'}</strong></span>
        <span>ATR: <strong className="text-slate-200">{advancedOverlays ? formatValue(latestValue(indicatorData, 'atr')) : 'advanced'}</strong></span>
      </div>
      {advancedOverlays && (
        <div className="grid gap-3 lg:grid-cols-3">
          <div className="card h-48 p-3"><h4 className="mb-2 text-sm font-semibold">RSI</h4><ResponsiveContainer width="100%" height="82%"><LineChart data={momentumData}><XAxis dataKey="index" hide /><YAxis domain={[0, 100]} /><Tooltip /><Line dot={false} dataKey="rsi" stroke="#f0b90b" /></LineChart></ResponsiveContainer></div>
          <div className="card h-48 p-3"><h4 className="mb-2 text-sm font-semibold">MACD</h4><ResponsiveContainer width="100%" height="82%"><LineChart data={momentumData}><XAxis dataKey="index" hide /><YAxis /><Tooltip /><Line dot={false} dataKey="macd" stroke="#16c784" /><Line dot={false} dataKey="signal" stroke="#ef4444" /></LineChart></ResponsiveContainer></div>
          <div className="card p-3 text-xs text-slate-300"><h4 className="mb-2 text-sm font-semibold text-slate-100">Advanced readout</h4><p>EMA 9: {formatValue(latestValue(indicatorData, 'ema_fast'))}</p><p>EMA 21: {formatValue(latestValue(indicatorData, 'ema_slow'))}</p><p>EMA 50: {formatValue(latestValue(indicatorData, 'ema_trend'))}</p><p>EMA 200: {formatValue(latestValue(indicatorData, 'ema_200'))}</p><p>OBV: {formatValue(latestValue(indicatorData, 'obv'), 0)}</p><p>CMF: {formatValue(latestValue(indicatorData, 'cmf'), 3)}</p><p>MFI: {formatValue(latestValue(indicatorData, 'mfi'), 1)}</p></div>
        </div>
      )}
    </div>
  )
}
