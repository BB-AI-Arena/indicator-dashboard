import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

function money(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : 'Unavailable'
}

function value(row, key) {
  return row?.[key] ?? 'Unavailable'
}

function SignalCard({ title, signal }) {
  if (!signal) {
    const empty = title.includes('Long') ? 'There is no active long signal at the moment.' : title.includes('Short') ? 'There is no active short signal at the moment.' : 'There is no active signal at the moment.'
    return <section className="card p-5"><div className="section-kicker">{title}</div><div className="mt-3 text-sm text-slate-400">{empty}</div></section>
  }
  const target1 = signal.targets?.[0]?.price
  return (
    <section className="card border border-emerald-500/20 p-5">
      <div className="flex items-start justify-between gap-3">
        <div><div className="section-kicker">{title}</div><h2 className="mt-1 text-xl font-semibold text-white">{signal.ticker} <span className="text-emerald-300">{signal.direction}</span></h2></div>
        <span className="status-chip status-chip-ok">{signal.state}</span>
      </div>
      <p className="mt-3 text-sm text-slate-300">{signal.thesis || 'Deterministic setup is active and awaiting the stated entry condition.'}</p>
      <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <div><span className="text-slate-500">Entry</span><div className="font-medium text-white">{signal.entry?.condition || money(signal.entry?.price)}</div></div>
        <div><span className="text-slate-500">Invalidation</span><div className="font-medium text-rose-300">{money(signal.invalidation?.price)}</div></div>
        <div><span className="text-slate-500">Target 1</span><div className="font-medium text-emerald-300">{money(target1)}</div></div>
        <div><span className="text-slate-500">Expires</span><div className="font-medium text-white">{signal.valid_until ? new Date(signal.valid_until).toLocaleTimeString() : 'Unavailable'}</div></div>
      </div>
      <div className="mt-4 border-t border-slate-800 pt-3 text-sm text-slate-300"><strong>Next action:</strong> {signal.next_action || `Wait for ${signal.entry?.condition || 'the exact entry condition'}`}</div>
    </section>
  )
}

function SignalRow({ signal, onSelect }) {
  return (
    <button className="w-full border-b border-slate-800/80 px-3 py-3 text-left transition hover:bg-slate-900/70" onClick={() => onSelect(signal)}>
      <div className="grid grid-cols-[auto_1fr_auto] items-center gap-3">
        <div><div className="font-semibold text-white">{signal.ticker}</div><div className={`text-xs ${signal.direction === 'LONG' ? 'text-emerald-300' : 'text-rose-300'}`}>{signal.direction}</div></div>
        <div className="min-w-0"><div className="truncate text-sm text-slate-200">{signal.setup_type}</div><div className="truncate text-xs text-slate-500">{signal.next_action || signal.entry?.condition}</div></div>
        <div className="text-right"><div className="text-xs text-slate-400">{signal.state}</div><div className="text-sm text-white">{money(signal.current_price)}</div></div>
      </div>
    </button>
  )
}

export default function ActiveSignals() {
  const [payload, setPayload] = useState(null)
  const [history, setHistory] = useState([])
  const [selected, setSelected] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  const load = async (refresh = false) => {
    try {
      if (refresh) setRefreshing(true)
      const [active, past] = await Promise.all([api.activeSignals(refresh), api.signalHistory(50)])
      setPayload(active)
      setHistory(past.signals || [])
      setError('')
    } catch (e) {
      setError(e.message || 'Unable to load active signals')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(() => load(false), 30000)
    return () => clearInterval(timer)
  }, [])

  const signals = payload?.active_signals || []
  const session = payload?.market_session
  const selectedSignal = selected || signals[0]
  const statusText = useMemo(() => payload?.message || `${signals.length} active signal${signals.length === 1 ? '' : 's'} passing deterministic gates`, [payload, signals.length])

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div><div className="section-kicker">Real-time options signal generator</div><h1 className="mt-1 text-2xl font-semibold text-white">ACTIVE SIGNALS</h1><p className="mt-1 text-sm text-slate-400">Only time-bounded signals with exact entry, invalidation, targets, and contract gates appear here.</p></div>
        <button className="button-secondary" onClick={() => load(true)} disabled={refreshing}>{refreshing ? 'Scanning...' : 'Run signal scan'}</button>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="card p-4"><div className="section-kicker">Market state</div><div className="mt-2 text-lg font-semibold text-white">{session?.session_state || 'Loading'}</div><div className="mt-1 text-xs text-slate-500">{session?.actionable_live_quotes ? 'Live option quotes may be actionable.' : 'Planning mode; no new executable intraday signals.'}</div></div>
        <div className="card p-4"><div className="section-kicker">Active count</div><div className="mt-2 text-lg font-semibold text-white">{payload?.active_count ?? '—'} / 10</div><div className="mt-1 text-xs text-slate-500">{payload?.last_full_scan ? `Last validation ${new Date(payload.last_full_scan).toLocaleTimeString()}` : 'No validation yet'}</div></div>
        <div className="card p-4"><div className="section-kicker">Signal status</div><div className="mt-2 text-sm text-slate-200">{loading ? 'Loading deterministic signals...' : statusText}</div>{error && <div className="mt-1 text-xs text-rose-300">{error}</div>}</div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <SignalCard title="Best Active Long Signal" signal={payload?.best_active_long} />
        <SignalCard title="Best Active Short Signal" signal={payload?.best_active_short} />
        <SignalCard title="Best 15-Minute Opportunity" signal={payload?.best_next_15m} />
      </div>

      {!loading && !signals.length && <div className="card border border-amber-500/20 p-8 text-center text-lg text-amber-200">There is nothing good at the moment. I am still working.</div>}

      {signals.length > 0 && <div className="grid gap-5 xl:grid-cols-[1.2fr_.8fr]">
        <section className="card overflow-hidden"><div className="border-b border-slate-800 px-4 py-3"><div className="section-kicker">Current signal stream</div><h2 className="mt-1 text-lg font-semibold text-white">Next 15 minutes</h2></div>{signals.map(signal => <SignalRow key={signal.signal_id} signal={signal} onSelect={setSelected} />)}</section>
        {selectedSignal && <section className="card p-5"><div className="section-kicker">Signal detail</div><div className="mt-1 flex items-center justify-between"><h2 className="text-xl font-semibold text-white">{selectedSignal.ticker} {selectedSignal.direction}</h2><span className="status-chip status-chip-ok">{selectedSignal.state}</span></div><div className="mt-4 space-y-3 text-sm"><div><span className="text-slate-500">Setup</span><div className="text-white">{selectedSignal.setup_type}</div></div><div><span className="text-slate-500">Exact entry</span><div className="text-white">{selectedSignal.entry?.condition}</div></div><div><span className="text-slate-500">Maximum chase</span><div className="text-white">{money(selectedSignal.maximum_chase_underlying)}</div></div><div><span className="text-slate-500">Invalidation</span><div className="text-rose-300">{selectedSignal.invalidation?.condition}</div></div><div><span className="text-slate-500">Targets</span><div className="text-emerald-300">{(selectedSignal.targets || []).slice(0, 2).map(row => money(row.price)).join(' / ') || 'Unavailable'}</div></div><div><span className="text-slate-500">Preferred contract</span><div className="text-white">{selectedSignal.preferred_option_contract?.contract || 'Unavailable'}</div><div className="text-xs text-slate-500">Maximum premium: {money(selectedSignal.preferred_option_contract?.maximum_acceptable_premium)}</div></div><div><span className="text-slate-500">AI validation</span><div className="text-white">{selectedSignal.ai_validation?.status || 'Unavailable'}</div></div><div><span className="text-slate-500">Primary conflict</span><div className="text-amber-200">{selectedSignal.primary_conflict || 'None recorded'}</div></div><div className="border-t border-slate-800 pt-3"><strong className="text-white">Next action:</strong> <span className="text-slate-300">{selectedSignal.next_action}</span></div></div></section>}
      </div>}

      <details className="card p-5"><summary className="cursor-pointer text-sm font-semibold text-white">Signal History ({history.length})</summary><div className="mt-4 overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-slate-500"><tr><th className="p-2">Ticker</th><th className="p-2">Setup</th><th className="p-2">Final state</th><th className="p-2">Reason</th><th className="p-2">Updated</th></tr></thead><tbody>{history.map(row => <tr key={row.signal_id} className="border-t border-slate-800"><td className="p-2 text-white">{row.ticker} {row.direction}</td><td className="p-2 text-slate-300">{row.setup_type}</td><td className="p-2 text-slate-300">{row.state}</td><td className="p-2 text-slate-400">{row.removal_reason || '—'}</td><td className="p-2 text-slate-500">{row.last_validated_at ? new Date(row.last_validated_at).toLocaleString() : '—'}</td></tr>)}</tbody></table>{!history.length && <div className="mt-3 text-sm text-slate-500">No terminal signals recorded yet.</div>}</div></details>
    </div>
  )
}
