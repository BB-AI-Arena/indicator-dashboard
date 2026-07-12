import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

function valueOrDash(value) {
  return value === null || value === undefined || value === '' ? '-' : value
}

function Stat({ label, value, tone = 'text-slate-100' }) {
  return (
    <div className="rounded border border-slate-700 bg-panel2 p-3">
      <p className="text-xs text-slate-400">{label}</p>
      <p className={`text-lg font-semibold ${tone}`}>{valueOrDash(value)}</p>
    </div>
  )
}

export default function HistoricalData() {
  const [status, setStatus] = useState(null)
  const [providers, setProviders] = useState([])
  const [dbStatus, setDbStatus] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try {
      const [backfillRes, providersRes, dbRes] = await Promise.all([
        api.backfillStatus(),
        api.providersStatus(),
        api.dbStatus(),
      ])
      setStatus(backfillRes)
      setProviders(providersRes.providers || [])
      setDbStatus(dbRes)
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 10000)
    return () => clearInterval(id)
  }, [])

  const run = status?.active_run || null
  const progress = useMemo(() => {
    const total = Number(status?.chunks_total || 0)
    const complete = Number(status?.chunks_complete || 0)
    return total > 0 ? Math.round((complete / total) * 100) : 0
  }, [status])

  const startBackfill = async () => {
    setBusy(true)
    setError('')
    try {
      await api.startBackfill({ all_symbols: true })
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const cancelBackfill = async () => {
    setBusy(true)
    setError('')
    try {
      await api.cancelBackfill()
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="card p-4">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-xl font-semibold">Historical Data</h2>
            <p className="text-sm text-amber-300">
              Intraday history may be limited by the selected provider. The app stores what is available and builds more history over time.
            </p>
            <p className="mt-1 text-sm text-slate-300">
              Backfill queues every active watchlist ticker, skips ranges already stored in SQLite, and resumes incomplete chunks.
            </p>
          </div>
          <div className="flex gap-2">
            <button disabled={busy || status?.running} className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" onClick={startBackfill}>
              Backfill All Watchlist
            </button>
            <button disabled={busy || !status?.active_run || run?.status === 'CANCELLED'} className="rounded bg-red-900/50 px-3 py-2 text-sm font-semibold text-red-100 disabled:opacity-50" onClick={cancelBackfill}>
              Cancel
            </button>
          </div>
        </div>
        {error && <p className="mt-3 text-sm text-bear">{error}</p>}
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <Stat label="Backfill Status" value={run?.status || 'IDLE'} tone={status?.running ? 'text-amber-300' : 'text-slate-100'} />
        <Stat label="Current Provider" value={run?.current_provider || status?.current_provider} />
        <Stat label="Current Symbol" value={status?.current_symbol || run?.current_symbol} />
        <Stat label="Current Interval" value={status?.current_interval || run?.current_interval} />
      </div>

      <div className="card p-4">
        <div className="mb-2 flex items-center justify-between text-sm">
          <span className="font-semibold text-slate-200">Chunks complete / total</span>
          <span>{status?.chunks_complete || 0} / {status?.chunks_total || 0}</span>
        </div>
        <div className="h-2 overflow-hidden rounded bg-slate-800">
          <div className="h-full bg-accent" style={{ width: `${progress}%` }} />
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-4">
          <Stat label="Failed Chunks" value={status?.failed_chunks || 0} tone={status?.failed_chunks ? 'text-bear' : 'text-slate-100'} />
          <Stat label="Rows Inserted" value={status?.rows_inserted || 0} />
          <Stat label="Rows Updated" value={status?.rows_updated || 0} />
          <Stat
            label="Current Throttle Delay"
            value={`${status?.current_throttle_delay?.between_symbols ?? 5}s symbols / ${status?.current_throttle_delay?.between_intervals ?? 10}s intervals`}
          />
        </div>
        {run?.message && <p className="mt-3 text-sm text-slate-300">{run.message}</p>}
      </div>

      <div className="card p-4">
        <h3 className="mb-3 text-lg font-semibold">Provider Rate Limits</h3>
        <div className="overflow-auto">
          <table className="min-w-full text-xs">
            <thead>
              <tr>
                {['Provider', 'RPM', 'RPH', 'Min Gap', 'Last Request', 'Backoff', 'Errors', 'Available'].map((h) => (
                  <th className="px-2 py-1 text-left" key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {providers.map((provider) => (
                <tr key={provider.provider} className="border-t border-slate-800">
                  <td className="px-2 py-1 font-semibold">{provider.provider}</td>
                  <td className="px-2 py-1">{valueOrDash(provider.requests_per_minute)}</td>
                  <td className="px-2 py-1">{valueOrDash(provider.requests_per_hour)}</td>
                  <td className="px-2 py-1">{valueOrDash(provider.min_seconds_between_requests)}s</td>
                  <td className="px-2 py-1">{valueOrDash(provider.last_request_time)}</td>
                  <td className="px-2 py-1">{provider.current_backoff_state?.active ? `${provider.current_backoff_state.remaining_seconds}s` : '-'}</td>
                  <td className="px-2 py-1">{provider.recent_error_count || 0}</td>
                  <td className={`px-2 py-1 font-semibold ${provider.available ? 'text-emerald-300' : 'text-amber-300'}`}>{provider.available ? 'Yes' : 'No'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card p-4">
          <h3 className="mb-2 text-lg font-semibold">SQLite Storage</h3>
          <div className="grid gap-2 text-sm sm:grid-cols-2">
            <div>Candles: <span className="font-semibold">{dbStatus?.candles ?? '-'}</span></div>
            <div>Candle Symbols: <span className="font-semibold">{dbStatus?.candle_symbols ?? '-'}</span></div>
            <div>Active Watchlist: <span className="font-semibold">{dbStatus?.active_watchlist_symbols ?? '-'}</span></div>
            <div>Scans: <span className="font-semibold">{dbStatus?.scans ?? '-'}</span></div>
            <div>Backfill Runs: <span className="font-semibold">{dbStatus?.backfill_runs ?? '-'}</span></div>
            <div>Backfill Chunks: <span className="font-semibold">{dbStatus?.backfill_chunks ?? '-'}</span></div>
          </div>
          {dbStatus?.candle_intervals?.length ? (
            <div className="mt-3 space-y-1 text-sm text-slate-300">
              {dbStatus.candle_intervals.map((row) => (
                <p key={row.interval}>{row.interval}: {row.rows} rows across {row.symbols} symbols</p>
              ))}
            </div>
          ) : null}
        </div>

        <div className="card p-4">
          <h3 className="mb-2 text-lg font-semibold">Last Provider Error</h3>
          {status?.last_provider_error ? (
            <div className="space-y-1 text-sm text-amber-300">
              <p>{status.last_provider_error.provider} {status.last_provider_error.symbol || ''} {status.last_provider_error.endpoint || ''}</p>
              <p>{status.last_provider_error.error_type}: {status.last_provider_error.error_message}</p>
              <p className="text-xs text-slate-400">{status.last_provider_error.created_at}</p>
            </div>
          ) : (
            <p className="text-sm text-slate-400">No provider errors recorded.</p>
          )}
        </div>
      </div>
    </div>
  )
}
