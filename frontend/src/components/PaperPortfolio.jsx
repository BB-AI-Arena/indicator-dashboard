import { useEffect, useState } from 'react'
import { api } from '../api'
import RecommendationPerformance from './RecommendationPerformance'
import ExitManagementPanel from './ExitManagementPanel'
import OptionEstimatePanel from './OptionEstimatePanel'

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `$${Number(value).toFixed(2)}`
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${Number(value).toFixed(1)}%`
}

export default function PaperPortfolio() {
  const [data, setData] = useState(null)
  const [estimateData, setEstimateData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      setError('')
      const [portfolioResult, estimatesResult] = await Promise.allSettled([
        api.paperPortfolio(),
        api.optionEstimates('', 200),
      ])
      if (portfolioResult.status === 'rejected') throw portfolioResult.reason
      setData(portfolioResult.value)
      if (estimatesResult.status === 'fulfilled') setEstimateData(estimatesResult.value)
    } catch (err) {
      setError(err.message || 'Paper Portfolio unavailable')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 60000)
    return () => clearInterval(id)
  }, [])

  const portfolio = data?.portfolio || {}
  const risk = data?.paper_risk || {}
  const positions = data?.positions || []
  const orders = data?.orders || []
  const riskByPosition = Object.fromEntries((risk.positions || []).map((row) => [row.position_id, row]))
  const estimatesBySymbol = Object.fromEntries((estimateData?.estimates || []).map((row) => [String(row.option_symbol || '').toUpperCase(), row]))

  if (loading && !data) return <div className="card p-4">Loading Paper Portfolio...</div>

  return (
    <div className="space-y-4">
      <section className="card border-emerald-800/60 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="decision-kicker">PAPER CHALLENGE</p>
            <h2 className="text-xl font-semibold">Paper Portfolio</h2>
            <p className="mt-1 text-sm text-slate-400">Simulated orders, fills, risk controls, and recommendation outcomes. This view never reads E*TRADE positions or balances.</p>
          </div>
          <button className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900" onClick={load}>Refresh</button>
        </div>
        {error && <p className="mt-3 text-sm text-red-300">{error}</p>}
      </section>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
        <div className="card p-4"><p className="text-xs uppercase text-slate-400">Starting balance</p><p className="mt-1 text-xl font-bold">{money(portfolio.starting_balance || 100000)}</p></div>
        <div className="card p-4"><p className="text-xs uppercase text-slate-400">Paper equity</p><p className="mt-1 text-xl font-bold">{money(portfolio.equity)}</p></div>
        <div className="card p-4"><p className="text-xs uppercase text-slate-400">Simulated cash</p><p className="mt-1 text-xl font-bold">{money(portfolio.cash)}</p></div>
        <div className="card p-4"><p className="text-xs uppercase text-slate-400">Paper P&amp;L</p><p className={`mt-1 text-xl font-bold ${Number(portfolio.realized_pnl) + Number(portfolio.unrealized_pnl) >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>{money(Number(portfolio.realized_pnl || 0) + Number(portfolio.unrealized_pnl || 0))}</p></div>
        <div className="card p-4"><p className="text-xs uppercase text-slate-400">Open paper positions</p><p className="mt-1 text-xl font-bold">{portfolio.position_count || 0}</p></div>
        <div className="card p-4"><p className="text-xs uppercase text-slate-400">Challenge return</p><p className={`mt-1 text-xl font-bold ${Number(portfolio.return_pct) >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>{pct(portfolio.return_pct)}</p></div>
      </div>

      <RecommendationPerformance performance={data?.recommendation_performance} compact />

      <section className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div><p className="decision-kicker">Simulated Risk Controls</p><h3 className="text-lg font-semibold">Deployment and exits</h3></div>
          <span className="badge border border-emerald-700/60 bg-emerald-900/30 text-emerald-200">PAPER ONLY</span>
        </div>
        <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <div>Capital deployed: <strong>{money(risk.capital_deployed)} / {money(risk.maximum_capital_deployed)}</strong></div>
          <div>Deployment: <strong>{pct(risk.deployment_pct)}</strong></div>
          <div>Realistic open risk: <strong>{money(risk.realistic_open_risk)}</strong></div>
          <div>Reserve: <strong>{pct(risk.reserve_pct)}</strong></div>
          <div>Profit trails active: <strong>{risk.profit_trails_active ?? 0}</strong></div>
          <div>Losing positions to close: <strong>{risk.losing_positions_requiring_same_day_close ?? 0}</strong></div>
          <div>Overnight holds approved: <strong>{risk.overnight_holds_approved ?? 0}</strong></div>
          <div>Orders: <strong>{orders.length}</strong></div>
        </div>
        {risk.deployment_warning && <p className="mt-3 text-sm text-amber-200">{risk.deployment_warning}</p>}
      </section>

      <section className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div><p className="decision-kicker">Paper Trade History</p><h3 className="text-lg font-semibold">Simulated orders and fills</h3></div>
          <span className="text-xs text-slate-500">{(data?.equity_curve || []).length} equity snapshots</span>
        </div>
        {(data?.trade_history || []).length === 0 ? <p className="mt-3 text-sm text-slate-400">No simulated trades yet.</p> : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-sm"><thead className="text-xs uppercase text-slate-500"><tr><th className="p-2">Time</th><th className="p-2">Symbol</th><th className="p-2">Side</th><th className="p-2">Qty</th><th className="p-2">Fill</th><th className="p-2">Status</th></tr></thead><tbody>
              {(data.trade_history || []).slice(0, 25).map((row) => <tr key={row.order_id} className="border-t border-slate-800"><td className="p-2 text-slate-400">{row.created_at}</td><td className="p-2">{row.symbol}</td><td className="p-2">{row.side}</td><td className="p-2">{row.quantity}</td><td className="p-2">{money(row.limit_price)}</td><td className="p-2">{row.status}</td></tr>)}
            </tbody></table>
          </div>
        )}
      </section>

      <section className="card p-4">
        <p className="decision-kicker">Simulated Positions</p>
        {positions.length === 0 ? <p className="mt-3 text-sm text-slate-400">No simulated positions. Real E*TRADE positions are intentionally excluded.</p> : (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {positions.map((position) => (
              <div key={position.position_id} className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
                <div className="flex justify-between"><strong>{position.display_symbol || position.symbol}</strong><span>{position.direction}</span></div>
                <p className="mt-2">Quantity: {position.quantity} • Entry: {money(position.entry_option_price)} • Current: {money(position.current_price)}</p>
                {position.fill_quality && <p className="mt-1 text-xs text-amber-200">Fill assumption: {position.fill_quality.replaceAll('_', ' ')} ({position.adverse_fill_penalty_pct ?? 5}% adverse) · intended {money(position.intended_fill_price)} → executed {money(position.executed_fill_price)}</p>}
                <p>Cost basis: {money(position.cost_basis)} • Market value: {money(position.market_value)}</p>
                <p className="mt-1 text-xs text-slate-500">Source: {position.simulated_fill_source || 'PAPER_SIMULATION'} • Recommendation: {position.recommendation_id || '-'}</p>
                <OptionEstimatePanel estimate={estimatesBySymbol[String(position.contract_symbol || '').toUpperCase()]} quantity={position.quantity} averageCost={position.cost_basis} />
                <ExitManagementPanel position={position} riskManagement={riskByPosition[position.position_id]} />
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
