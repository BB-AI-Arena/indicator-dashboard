import { useEffect, useState } from 'react'
import { api } from '../api'
import RecommendationPerformance from './RecommendationPerformance'
import ExitManagementPanel from './ExitManagementPanel'

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
  const [morning, setMorning] = useState(null)
  const [error, setError] = useState('')
  const [morningError, setMorningError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      setError('')
      setMorningError('')
      const [portfolioResult, morningResult] = await Promise.allSettled([
        api.paperPortfolio(),
        api.paperMorningBrief(),
      ])
      if (portfolioResult.status === 'fulfilled') setData(portfolioResult.value)
      else setError(portfolioResult.reason?.message || 'Paper Portfolio unavailable')
      if (morningResult.status === 'fulfilled') setMorning(morningResult.value)
      else setMorningError(morningResult.reason?.message || 'Morning brief unavailable')
    } catch (err) {
      setError(err.message || 'Paper Portfolio unavailable')
    } finally {
      setLoading(false)
    }
  }

  const refreshMorning = async () => {
    try {
      setMorningError('')
      setMorning(await api.paperMorningRefresh())
    } catch (err) {
      setMorningError(err.message || 'Morning brief unavailable')
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

      <section className="card border-sky-800/60 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="decision-kicker">PAPER ONLY / PREMARKET PLAN</p>
            <h3 className="text-lg font-semibold">Morning Trading Brief</h3>
            <p className="mt-1 text-sm text-slate-400">
              Stored market data first. No candidate becomes a paper trade until the opening setup confirms.
            </p>
          </div>
          <button className="rounded border border-sky-700/70 bg-sky-950/40 px-3 py-2 text-sm font-semibold text-sky-200" onClick={refreshMorning}>
            Refresh Brief
          </button>
        </div>
        {morningError && <p className="mt-3 text-sm text-red-300">{morningError}</p>}
        {morning && (
          <>
            <div className="mt-4 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-5">
              <div><span className="text-slate-500">Session</span><strong className="ml-2">{morning.session?.session_state || '-'}</strong></div>
              <div><span className="text-slate-500">Next open</span><strong className="ml-2">{morning.session?.next_market_open || '-'}</strong></div>
              <div><span className="text-slate-500">Regime</span><strong className="ml-2">{morning.market?.regime || 'UNAVAILABLE'}</strong></div>
              <div><span className="text-slate-500">SPY / QQQ</span><strong className="ml-2">{morning.market?.spy_trend || '-'} / {morning.market?.qqq_trend || '-'}</strong></div>
              <div><span className="text-slate-500">Refreshed</span><strong className="ml-2">{morning.last_refresh || '-'}</strong></div>
            </div>
            {morning.overall_message && <div className="mt-4 rounded border border-amber-700/60 bg-amber-950/30 p-3 text-sm text-amber-100">{morning.overall_message}</div>}
            {!morning.overall_message && (
              <div className="mt-4 grid gap-3 lg:grid-cols-2">
                {[['best_long', morning.best_long, morning.best_long_label], ['best_short', morning.best_short, morning.best_short_label]].map(([key, row, label]) => (
                  <div key={key} className="rounded border border-slate-700 bg-panel2 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="decision-kicker">{label}</p>
                      {row && <span className="badge border border-slate-700 bg-slate-900/40 text-slate-200">{row.status}</span>}
                    </div>
                    {row ? (
                      <>
                        <div className="mt-2 flex items-baseline justify-between gap-2"><strong className="text-xl">{row.ticker}</strong><span className="text-sm text-slate-300">{row.setup}</span></div>
                        <p className="mt-2 text-sm text-slate-300">{row.catalyst?.headline || row.reason_included}</p>
                        <div className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
                          <span>Gap: <strong>{row.gap?.gap_pct == null ? '-' : row.gap.gap_pct.toFixed(2) + '%'}</strong></span>
                          <span>Premarket RVOL: <strong>{row.premarket?.rvol == null ? '-' : row.premarket.rvol + 'x'}</strong></span>
                          <span>Liquidity: <strong>{row.option_liquidity_status}</strong></span>
                        </div>
                        <p className="mt-3 text-sm"><strong>Trigger:</strong> {row.entry_trigger?.condition || 'Wait for a completed opening confirmation.'}</p>
                        <p className="mt-1 text-sm"><strong>Invalidation:</strong> {row.invalidation?.condition || '-'}</p>
                        <p className="mt-1 text-sm"><strong>Primary risk:</strong> {row.primary_risk}</p>
                      </>
                    ) : <p className="mt-3 text-sm text-slate-400">{label}</p>}
                  </div>
                ))}
              </div>
            )}
            <div className="mt-4">
              <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="font-semibold">Top Morning Candidates</h4><span className="text-xs text-slate-500">{(morning.candidates || []).length} of 10 maximum</span></div>
              {(morning.candidates || []).length === 0 ? <p className="mt-3 text-sm text-slate-400">No stored candidates are available yet.</p> : (
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full min-w-[900px] text-left text-sm">
                    <thead className="text-xs uppercase text-slate-500"><tr><th className="p-2">#</th><th className="p-2">Ticker</th><th className="p-2">Bias</th><th className="p-2">State</th><th className="p-2">Catalyst</th><th className="p-2">Gap</th><th className="p-2">Premarket RVOL</th><th className="p-2">Levels</th><th className="p-2">Options</th><th className="p-2">Risk</th></tr></thead>
                    <tbody>
                      {(morning.candidates || []).map((row, index) => (
                        <tr key={row.ticker + '-' + index} className="border-t border-slate-800 align-top">
                          <td className="p-2 text-slate-500">{index + 1}</td>
                          <td className="p-2 font-semibold">{row.ticker}</td>
                          <td className="p-2">{row.direction_bias || '-'}</td>
                          <td className="p-2"><span className="badge border border-slate-700 bg-slate-900/40 text-slate-200">{row.status}</span></td>
                          <td className="p-2">{row.catalyst?.category || '-'} / {row.catalyst?.strength || '-'}</td>
                          <td className="p-2">{row.gap?.gap_pct == null ? '-' : row.gap.gap_pct.toFixed(2) + '%'}<br /><span className="text-xs text-slate-500">{row.gap?.classification || '-'}</span></td>
                          <td className="p-2">{row.premarket?.rvol == null ? '-' : row.premarket.rvol + 'x'}<br /><span className="text-xs text-slate-500">{row.premarket?.status || '-'}</span></td>
                          <td className="p-2 text-xs">S {money(row.levels?.support)}<br />R {money(row.levels?.resistance)}</td>
                          <td className="p-2 text-xs">{row.option_liquidity_status}</td>
                          <td className="p-2 text-xs text-slate-400">{row.primary_risk}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div className="mt-4 grid gap-2 lg:grid-cols-3">
              {(morning.candidates || []).slice(0, 3).map((row) => (
                <details key={row.ticker} className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
                  <summary className="cursor-pointer font-semibold">{row.ticker} opening scenarios</summary>
                  <div className="mt-2 space-y-2 text-slate-400">
                    {(row.opening_scenarios || []).map((scenario) => (
                      <div key={scenario.name}><strong className="text-slate-200">{scenario.name}:</strong> {scenario.condition}<br /><span className="text-xs">{scenario.action}</span></div>
                    ))}
                  </div>
                </details>
              ))}
            </div>
            <div className="mt-4 grid gap-3 lg:grid-cols-2">
              <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm"><strong>Opening discipline</strong><p className="mt-1 text-slate-400">Confirmation timeframe: 5m default / 15m context. No automatic 9:30 AM entries. Wait for breakout-and-hold, pullback-and-hold, or a separately qualified reversal.</p></div>
              <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm"><strong>No-trade conditions</strong><ul className="mt-1 list-disc pl-5 text-slate-400">{(morning.no_trade_conditions || []).slice(0, 6).map((item) => <li key={item}>{item}</li>)}</ul></div>
            </div>
          </>
        )}
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
                <p>Cost basis: {money(position.cost_basis)} • Market value: {money(position.market_value)}</p>
                <p className="mt-1 text-xs text-slate-500">Source: {position.simulated_fill_source || 'PAPER_SIMULATION'} • Recommendation: {position.recommendation_id || '-'}</p>
                <ExitManagementPanel position={position} riskManagement={riskByPosition[position.position_id]} />
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
