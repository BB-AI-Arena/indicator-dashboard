import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import TickerSearch from './TickerSearch'
import HistoricalData from './HistoricalData'
import { formatCentralTime } from '../utils/time'

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${(Number(value) * 100).toFixed(1)}%`
}

function pctRaw(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${Number(value).toFixed(2)}%`
}

function statusClass(status) {
  const text = String(status || '').toUpperCase()
  if (text === 'READY_FOR_LIVE_ANALYSIS' || text === 'READY_FOR_PLANNING') return 'text-emerald-300'
  if (['BUILDING', 'PARTIAL', 'ANALYSIS_PENDING', 'STALE'].includes(text)) return 'text-amber-300'
  if (['BLOCKED', 'ERROR'].includes(text)) return 'text-red-300'
  return 'text-slate-200'
}

export default function WatchlistIntelligence({ onSelectSymbol }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setError('')
    try {
      const payload = await api.watchlistIntelligence()
      setData(payload)
    } catch (e) {
      setError(e.message || 'Watchlist intelligence unavailable')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const addWatchlist = async (symbol) => {
    const normalized = String(symbol || '').trim().toUpperCase()
    if (!normalized) return
    setError('')
    try {
      await api.addWatchlist(normalized)
      await load()
    } catch (e) {
      setError(e.message || 'Unable to add symbol')
    }
  }

  const removeWatchlist = async (symbol) => {
    setError('')
    try {
      await api.removeWatchlist(symbol)
      await load()
    } catch (e) {
      setError(e.message || 'Unable to remove symbol')
    }
  }

  const rows = useMemo(() => data?.rows || [], [data])

  return (
    <div className="space-y-4">
      <section className="card p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="decision-kicker">Watchlist Intelligence</p>
            <h2 className="text-2xl font-bold text-slate-50">Profiles, coverage, and cached analysis</h2>
            <p className="mt-1 text-sm text-slate-400">
              This tab holds the detailed watchlist, profile coverage, historical state, and provider-backed data diagnostics. The dashboard stays decision-only.
            </p>
          </div>
          <div className="text-xs text-slate-500">Updated {formatCentralTime(data?.generated_at)}</div>
        </div>
        <div className="mt-4">
          <TickerSearch onAnalyze={(symbol) => { addWatchlist(symbol); onSelectSymbol(symbol) }} onAddWatchlist={addWatchlist} />
        </div>
      </section>

      {loading && <div className="card p-4 text-slate-300">Loading watchlist intelligence...</div>}
      {error && <div className="card p-4 text-bear">{error}</div>}

      <section className="card overflow-hidden">
        <div className="border-b border-slate-800 px-4 py-3">
          <h3 className="font-semibold text-slate-100">Core Universe and Watchlist</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1200px] text-left text-sm">
            <thead className="bg-slate-950/70 text-xs uppercase text-slate-500">
              <tr>
                <th className="px-3 py-2">Ticker</th>
                <th className="px-3 py-2">Profile</th>
                <th className="px-3 py-2">Coverage</th>
                <th className="px-3 py-2">Trend</th>
                <th className="px-3 py-2">Next Bias</th>
                <th className="px-3 py-2">Setup</th>
                <th className="px-3 py-2">Hit Rate</th>
                <th className="px-3 py-2">EV</th>
                <th className="px-3 py-2">Options</th>
                <th className="px-3 py-2">News</th>
                <th className="px-3 py-2">Social</th>
                <th className="px-3 py-2">Last Earnings</th>
                <th className="px-3 py-2">Freshness</th>
                <th className="px-3 py-2">Hard Gates</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {rows.map((row) => {
                const intervals = row.historical_coverage?.intervals || {}
                const coverage = Object.entries(intervals).map(([interval, item]) => `${interval}: ${item.rows}`).join(' | ')
                return (
                  <tr key={row.ticker} className="hover:bg-slate-900/40">
                    <td className="px-3 py-3">
                      <button className="font-bold text-accent hover:text-emerald-200" onClick={() => onSelectSymbol(row.ticker)}>
                        {row.ticker}
                      </button>
                    </td>
                    <td className={`px-3 py-3 font-semibold ${statusClass(row.profile_state || row.profile_status)}`}>
                      <div>{row.profile_state || row.profile_status || '-'}</div>
                      <div className="text-xs font-normal text-slate-400">{row.completeness_percentage == null ? 'Pending' : `${Number(row.completeness_percentage).toFixed(0)}% complete`}</div>
                      {row.readiness?.next_required_job && <div className="text-xs font-normal text-amber-300">Next: {row.readiness.next_required_job}</div>}
                    </td>
                    <td className="px-3 py-3 text-xs text-slate-300">{coverage || '-'}</td>
                    <td className="px-3 py-3">{row.current_trend || '-'}</td>
                    <td className="px-3 py-3">{row.next_session_bias || '-'}</td>
                    <td className="px-3 py-3">{row.current_setup || '-'}</td>
                    <td className="px-3 py-3">{pct(row.historical_hit_rate)}</td>
                    <td className="px-3 py-3">{pctRaw(row.expected_value)}</td>
                    <td className="px-3 py-3">{row.options_positioning_classification || '-'}</td>
                    <td className="px-3 py-3">{row.news_impact || '-'}</td>
                    <td className="px-3 py-3 text-xs">
                      <div className="font-semibold text-slate-200">{row.social_classification || 'INSUFFICIENT DATA'}</div>
                      <div className="text-slate-400">{Number.isFinite(Number(row.social_sentiment_score)) ? `Score ${Number(row.social_sentiment_score).toFixed(0)}` : 'No score'}{row.social_mention_velocity ? ` • ${Number(row.social_mention_velocity).toFixed(1)}x` : ''}</div>
                    </td>
                    <td className="px-3 py-3 text-xs">
                      {row.last_earnings_date ? (
                        <>
                          <div className="font-semibold text-slate-200">{row.last_earnings_result || 'UNKNOWN'} • {row.last_earnings_date}</div>
                          <div className={Number(row.last_earnings_reaction) >= 0 ? 'text-emerald-300' : 'text-red-300'}>
                            Stock reaction: {Number.isFinite(Number(row.last_earnings_reaction)) ? `${Number(row.last_earnings_reaction).toFixed(2)}%` : 'unavailable'}
                          </div>
                        </>
                      ) : 'Unavailable'}
                    </td>
                    <td className="px-3 py-3 text-xs text-slate-400">{formatCentralTime(row.data_freshness?.latest_15m_candle)}</td>
                    <td className="px-3 py-3 text-xs text-amber-300">{row.hard_gates?.slice(0, 3).join(', ') || 'PASS'}</td>
                    <td className="px-3 py-3">
                      <button className="rounded border border-red-900/60 px-2 py-1 text-xs text-red-200 hover:bg-red-950/40" onClick={() => removeWatchlist(row.ticker)}>
                        Remove
                      </button>
                    </td>
                  </tr>
                )
              })}
              {!rows.length && !loading && (
                <tr><td className="px-3 py-4 text-slate-400" colSpan="15">No watchlist rows available.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card p-4">
        <p className="decision-kicker">Historical Tables and Provider Diagnostics</p>
        <div className="mt-3">
          <HistoricalData />
        </div>
      </section>
    </div>
  )
}
