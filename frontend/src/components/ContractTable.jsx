import { formatCentralTime } from '../utils/time'

export default function ContractTable({ contracts, side }) {
  const pct = (value) => Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)}%` : '-'
  const money = (value) => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '-'
  const renderRows = (rows) => rows?.slice(0, 8).map((c) => (
    <tr key={`${c.contract_symbol}-${c.expiration}-${c.type}`} className="border-t border-slate-800">
      <td className="px-2 py-1">{c.contract_symbol}</td>
      <td className="px-2 py-1">{c.type || '-'}</td>
      <td className="px-2 py-1">{c.expiration}</td>
      <td className="px-2 py-1">{c.strike}</td>
      <td className="px-2 py-1">{money(c.underlying_price)}</td>
      <td className="px-2 py-1">{c.moneyness || '-'}</td>
      <td className="px-2 py-1">{pct(c.distance_from_spot_pct)}</td>
      <td className="px-2 py-1">{c.bid}</td>
      <td className="px-2 py-1">{c.ask}</td>
      <td className="px-2 py-1">{pct(c.spread_percentage)}</td>
      <td className="px-2 py-1">{c.volume}</td>
      <td className="px-2 py-1">{c.open_interest}</td>
      <td className="px-2 py-1">{c.quote_type || '-'}</td>
      <td className="px-2 py-1">{formatCentralTime(c.quote_timestamp, { seconds: false })}</td>
      <td className="px-2 py-1">{c.expiration_risk || '-'}</td>
      <td className="px-2 py-1 font-bold">{c.liquidity_grade || '-'}</td>
      <td className="px-2 py-1 font-bold">{c.risk_grade || '-'}</td>
      <td className="px-2 py-1 font-bold">{c.trade_grade || c.grade || '-'}</td>
    </tr>
  ))

  const normalizedSide = (side || '').toUpperCase()
  const signal = contracts?.chart_signal || {}
  const chartConfirmed = (
    ['TRADE_CANDIDATE', 'HIGH_CONVICTION'].includes((signal.grade || '').toUpperCase()) &&
    signal.side === normalizedSide
  )
  let title = 'Top Liquid Option Candidates'
  let rows = []

  if (normalizedSide === 'LONG') {
    title = chartConfirmed ? 'Top Directionally Confirmed Call Candidates' : 'Top Liquid Call Candidates'
    rows = contracts?.calls || []
  } else if (normalizedSide === 'SHORT') {
    title = chartConfirmed ? 'Top Directionally Confirmed Put Candidates' : 'Top Liquid Put Candidates'
    rows = contracts?.puts || []
  } else {
    title = 'Top Liquid Option Candidates (No Clear Bias)'
    rows = [...(contracts?.calls || []), ...(contracts?.puts || [])]
      .sort((a, b) => Number(b?.score || 0) - Number(a?.score || 0))
  }

  return (
    <div className="space-y-2">
      <div className="text-xs text-slate-400">
        Source: {contracts?.source || contracts?.provider || '-'} | Quote Type: {contracts?.quote_type || '-'} | Timestamp: {formatCentralTime(contracts?.timestamp)}
      </div>
      {contracts?.warning && <div className="rounded bg-amber-900/30 p-2 text-xs text-amber-300">{contracts.warning}</div>}
      <div className="card overflow-auto p-3">
        <h4 className="mb-2 font-semibold">{title}</h4>
        <table className="min-w-full text-xs">
          <thead>
            <tr>
              {[
                'Contract', 'Type', 'Exp', 'Strike', 'Spot', 'Mny', 'Dist%', 'Bid', 'Ask', 'Spread%',
                'Vol', 'OI', 'Quote', 'Quote Time', 'Exp Risk', 'Liq', 'Risk', 'Trade',
              ].map((h) => <th className="px-2 py-1 text-left" key={h}>{h}</th>)}
            </tr>
          </thead>
          <tbody>{renderRows(rows)}</tbody>
        </table>
      </div>
    </div>
  )
}
