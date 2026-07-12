import { useEffect, useState } from 'react'
import { api } from '../api'

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `$${Number(value).toFixed(2)}`
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${Number(value).toFixed(2)}%`
}

function formatTimestamp(value) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('en-US', { timeZone: 'America/New_York', hour12: false })
}

function CandidateCard({ candidate, label }) {
  if (!candidate) {
    return (
      <div className="rounded border border-slate-700 bg-panel2 p-4">
        <p className="decision-kicker">{label}</p>
        <p className="mt-3 text-sm text-slate-400">No qualified setup. The system is still monitoring.</p>
      </div>
    )
  }

  return (
    <div className="rounded border border-slate-700 bg-panel2 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="decision-kicker">{label}</p>
          <div className="mt-1 flex items-baseline gap-2"><h3 className="text-2xl font-semibold">{candidate.ticker}</h3><span className="text-sm text-slate-400">{candidate.direction_bias || 'UNDEFINED'}</span></div>
        </div>
        <span className="badge border border-sky-700/60 bg-sky-950/40 text-sky-200">{candidate.status}</span>
      </div>
      <p className="mt-3 text-sm text-slate-300">{candidate.catalyst?.headline || candidate.reason_included}</p>
      <div className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
        <div><span className="text-slate-500">Catalyst</span><strong className="ml-2">{candidate.catalyst?.category || 'NONE'} / {candidate.catalyst?.strength || '-'}</strong></div>
        <div><span className="text-slate-500">Gap</span><strong className="ml-2">{pct(candidate.gap?.gap_pct)}</strong></div>
        <div><span className="text-slate-500">Premarket RVOL</span><strong className="ml-2">{candidate.premarket?.rvol == null ? '-' : `${candidate.premarket.rvol}x`}</strong></div>
      </div>
      <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
        <div><span className="text-slate-500">Support</span><strong className="ml-2">{money(candidate.levels?.support)}</strong></div>
        <div><span className="text-slate-500">Resistance</span><strong className="ml-2">{money(candidate.levels?.resistance)}</strong></div>
        <div><span className="text-slate-500">Options</span><strong className="ml-2">{candidate.option_liquidity_status || '-'}</strong></div>
        <div><span className="text-slate-500">Confidence</span><strong className="ml-2">{candidate.confidence || '-'}</strong></div>
      </div>
      <div className="mt-4 space-y-2 text-sm">
        <p><strong>Opening plan:</strong> {candidate.opening_scenarios?.[0]?.condition || 'Wait for a completed opening confirmation.'}</p>
        <p><strong>Do not chase above:</strong> {money(candidate.chase_threshold)}</p>
        <p><strong>Primary risk:</strong> {candidate.primary_risk || 'Opening liquidity and confirmation are unknown.'}</p>
      </div>
    </div>
  )
}

export default function MorningSetup() {
  const [morning, setMorning] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    try {
      setError('')
      setMorning(await api.paperMorningBrief())
    } catch (err) {
      setError(err.message || 'Morning Setup unavailable')
    } finally {
      setLoading(false)
    }
  }

  const refresh = async () => {
    try {
      setRefreshing(true)
      setError('')
      setMorning(await api.paperMorningRefresh())
    } catch (err) {
      setError(err.message || 'Morning Setup refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 60000)
    return () => clearInterval(id)
  }, [])

  if (loading && !morning) return <div className="card p-4">Loading Morning Setup...</div>

  const candidates = morning?.candidates || []
  const session = morning?.session || {}
  const market = morning?.market || {}

  return (
    <div className="space-y-4">
      <section className="card border-sky-800/60 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="decision-kicker">PAPER ONLY / PREMARKET ROUTINE</p>
            <h2 className="text-2xl font-semibold">Morning Setup</h2>
            <p className="mt-1 max-w-3xl text-sm text-slate-400">A ranked premarket watchlist for the paper portfolio. This view plans opening scenarios; it never enters a trade at 9:30 and never changes real E*TRADE accounts.</p>
          </div>
          <button className="rounded border border-sky-700/70 bg-sky-950/40 px-3 py-2 text-sm font-semibold text-sky-200" onClick={refresh} disabled={refreshing}>
            {refreshing ? 'Refreshing...' : 'Refresh Morning Brief'}
          </button>
        </div>
        {error && <p className="mt-3 text-sm text-red-300">{error}</p>}
        <div className="mt-4 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-6">
          <div><span className="text-slate-500">Session</span><strong className="ml-2">{session.session_state || '-'}</strong></div>
          <div><span className="text-slate-500">Next open</span><strong className="ml-2">{formatTimestamp(session.next_market_open)}</strong></div>
          <div><span className="text-slate-500">Regime</span><strong className="ml-2">{market.regime || 'UNAVAILABLE'}</strong></div>
          <div><span className="text-slate-500">SPY</span><strong className="ml-2">{market.spy_trend || '-'}</strong></div>
          <div><span className="text-slate-500">QQQ</span><strong className="ml-2">{market.qqq_trend || '-'}</strong></div>
          <div><span className="text-slate-500">Updated</span><strong className="ml-2">{formatTimestamp(morning?.last_refresh)}</strong></div>
        </div>
      </section>

      {morning?.overall_message && <section className="card border-amber-700/60 bg-amber-950/20 p-4 text-sm text-amber-100">{morning.overall_message}</section>}

      <section className="grid gap-4 lg:grid-cols-2">
        <CandidateCard candidate={morning?.best_long} label={morning?.best_long_label || 'BEST LONG TO WATCH'} />
        <CandidateCard candidate={morning?.best_short} label={morning?.best_short_label || 'BEST SHORT TO WATCH'} />
      </section>

      <section className="card p-4">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div><p className="decision-kicker">RANKED ATTENTION LIST</p><h3 className="text-lg font-semibold">Top Morning Candidates</h3></div>
          <span className="text-xs text-slate-500">{candidates.length} of 10 maximum · immutable daily snapshots</span>
        </div>
        {candidates.length === 0 ? <p className="mt-3 text-sm text-slate-400">No stored candidates are available yet.</p> : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full min-w-[1050px] text-left text-sm">
              <thead className="text-xs uppercase text-slate-500"><tr><th className="p-2">#</th><th className="p-2">Ticker</th><th className="p-2">Bias</th><th className="p-2">State</th><th className="p-2">Catalyst</th><th className="p-2">Gap</th><th className="p-2">RVOL</th><th className="p-2">Key levels</th><th className="p-2">Options</th><th className="p-2">Confidence</th></tr></thead>
              <tbody>
                {candidates.map((row, index) => (
                  <tr key={`${row.ticker}-${index}`} className="border-t border-slate-800 align-top">
                    <td className="p-2 text-slate-500">{index + 1}</td>
                    <td className="p-2 font-semibold">{row.ticker}</td>
                    <td className="p-2">{row.direction_bias || '-'}</td>
                    <td className="p-2"><span className="badge border border-slate-700 bg-slate-900/40 text-slate-200">{row.status}</span></td>
                    <td className="p-2">{row.catalyst?.category || '-'} / {row.catalyst?.strength || '-'}</td>
                    <td className="p-2">{pct(row.gap?.gap_pct)}<br /><span className="text-xs text-slate-500">{row.gap?.classification || '-'}</span></td>
                    <td className="p-2">{row.premarket?.rvol == null ? '-' : `${row.premarket.rvol}x`}<br /><span className="text-xs text-slate-500">{row.premarket?.status || '-'}</span></td>
                    <td className="p-2 text-xs">S {money(row.levels?.support)}<br />R {money(row.levels?.resistance)}</td>
                    <td className="p-2 text-xs">{row.option_liquidity_status || '-'}</td>
                    <td className="p-2 text-xs">{row.confidence || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <div className="card p-4">
          <p className="decision-kicker">OPENING DISCIPLINE</p>
          <h3 className="text-lg font-semibold">Wait for the setup</h3>
          <p className="mt-2 text-sm text-slate-400">Use 15-minute context with a completed 5-minute confirmation by default. Require spread stabilization, VWAP or key-level support, volume confirmation, and acceptable reward-to-risk. No automatic market-open entries.</p>
        </div>
        <div className="card p-4">
          <p className="decision-kicker">NO-TRADE CONDITIONS</p>
          <h3 className="text-lg font-semibold">Reasons to stay in cash</h3>
          {(morning?.no_trade_conditions || []).length === 0 ? <p className="mt-2 text-sm text-slate-400">No system-level blocker was recorded.</p> : <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-400">{morning.no_trade_conditions.slice(0, 10).map((item) => <li key={item}>{item}</li>)}</ul>}
        </div>
      </section>

      <section className="card p-4">
        <p className="decision-kicker">OPENING SCENARIOS</p>
        <h3 className="text-lg font-semibold">What must happen next</h3>
        <div className="mt-3 grid gap-3 lg:grid-cols-3">
          {candidates.slice(0, 3).map((row) => (
            <details key={row.ticker} className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
              <summary className="cursor-pointer font-semibold">{row.ticker} · {row.direction_bias || 'NO BIAS'}</summary>
              <div className="mt-2 space-y-2 text-slate-400">{(row.opening_scenarios || []).map((scenario) => <div key={scenario.name}><strong className="text-slate-200">{scenario.name}:</strong> {scenario.condition}<br /><span className="text-xs">{scenario.action}</span></div>)}</div>
            </details>
          ))}
        </div>
      </section>
    </div>
  )
}
