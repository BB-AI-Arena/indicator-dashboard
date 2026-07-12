function sideColor(side) {
  if (side === 'LONG') return 'text-bull'
  if (side === 'SHORT') return 'text-bear'
  return 'text-neutral'
}

export default function SignalTable({ results, onSelect }) {
  return (
    <div className="card overflow-auto">
      <table className="min-w-full text-sm">
        <thead className="bg-panel2 text-slate-300">
          <tr>
            {['Ticker', 'Price', 'Bias', 'Score', 'Grade', 'RSI', 'VWAP', 'EMA Trend', 'P/C Ratio', 'Options Bias', 'Alert'].map((h) => <th key={h} className="px-3 py-2 text-left">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.symbol} className="border-t border-slate-800 hover:bg-slate-900/40 cursor-pointer" onClick={() => onSelect(r.symbol)}>
              <td className="px-3 py-2 font-semibold">{r.symbol}</td>
              <td className="px-3 py-2">{r.price?.toFixed?.(2) ?? r.price}</td>
              <td className={`px-3 py-2 font-semibold ${sideColor(r.side)}`}>{r.side}</td>
              <td className="px-3 py-2">{r.score}/{r.max_score}</td>
              <td className="px-3 py-2">{r.grade}</td>
              <td className="px-3 py-2">{r.indicators?.rsi ? r.indicators.rsi.toFixed(1) : '-'}</td>
              <td className="px-3 py-2">{r.indicators?.close > r.indicators?.vwap ? 'Above' : 'Below'}</td>
              <td className="px-3 py-2">{r.indicators?.ema_fast > r.indicators?.ema_slow ? 'Bull' : 'Bear'}</td>
              <td className="px-3 py-2">{r.option_ratios?.put_call_ratio ? r.option_ratios.put_call_ratio.toFixed(2) : '-'}</td>
              <td className="px-3 py-2">{r.option_ratios?.bias || '-'}</td>
              <td className="px-3 py-2">{r.alert ? 'YES' : 'NO'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
