export default function IndicatorCards({ scan, options }) {
  const i = scan?.indicators || {}
  const hasVwap = Number.isFinite(i?.close) && Number.isFinite(i?.vwap)
  const hasTrend = Number.isFinite(i?.ema_fast) && Number.isFinite(i?.ema_slow)
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="card p-3"><p className="text-xs text-slate-400">RSI</p><p className="text-xl font-bold">{i.rsi ? i.rsi.toFixed(2) : '-'}</p></div>
      <div className="card p-3"><p className="text-xs text-slate-400">VWAP Status</p><p className="text-xl font-bold">{hasVwap ? (i.close > i.vwap ? 'Above' : 'Below') : '-'}</p></div>
      <div className="card p-3"><p className="text-xs text-slate-400">MACD Hist</p><p className="text-xl font-bold">{i.macd_hist ? i.macd_hist.toFixed(4) : '-'}</p></div>
      <div className="card p-3"><p className="text-xs text-slate-400">EMA Trend</p><p className="text-xl font-bold">{hasTrend ? (i.ema_fast > i.ema_slow ? 'Bull' : 'Bear') : '-'}</p></div>
    </div>
  )
}
