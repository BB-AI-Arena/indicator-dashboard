import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatCentralTime } from '../utils/time'

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${(Number(value) * 100).toFixed(1)}%`
}

function pctRaw(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${Number(value).toFixed(2)}%`
}

function confidenceTone(confidence) {
  const text = String(confidence || '').toUpperCase()
  if (text === 'STRONG' || text === 'MODERATE') return 'border-emerald-700/70 bg-emerald-950/30 text-emerald-200'
  if (text === 'LOW') return 'border-amber-700/70 bg-amber-950/30 text-amber-200'
  return 'border-red-800/70 bg-red-950/30 text-red-200'
}

function ScopeStats({ title, scope }) {
  const examples = Number(scope?.examples || 0)
  return (
    <div className="rounded border border-slate-700/80 bg-slate-950/30 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-slate-100">{title}</p>
        <span className={`rounded border px-2 py-0.5 text-xs font-bold ${confidenceTone(scope?.confidence)}`}>
          {scope?.confidence || 'INSUFFICIENT'}
        </span>
      </div>
      <div className="mt-2 grid gap-2 text-xs text-slate-300 sm:grid-cols-2">
        <p>Examples: <span className="font-semibold text-slate-100">{examples}</span></p>
        <p>Target before invalidation: <span className="font-semibold text-slate-100">{scope?.successes || 0}/{examples}</span></p>
        <p>Raw hit rate: <span className="font-semibold text-slate-100">{pct(scope?.raw_success_rate)}</span></p>
        <p>Out-of-sample: <span className="font-semibold text-slate-100">{pct(scope?.out_of_sample_success_rate)}</span></p>
        <p>Average return: <span className="font-semibold text-slate-100">{pctRaw(scope?.average_return_pct)}</span></p>
        <p>Expected value: <span className={`font-semibold ${Number(scope?.expected_value_pct || 0) > 0 ? 'text-emerald-300' : 'text-red-300'}`}>{pctRaw(scope?.expected_value_pct)}</span></p>
      </div>
      {scope?.confidence_interval?.low !== null && scope?.confidence_interval?.low !== undefined ? (
        <p className="mt-2 text-xs text-slate-400">
          Wilson interval: {pct(scope.confidence_interval.low)} to {pct(scope.confidence_interval.high)}
        </p>
      ) : null}
      {scope?.warning ? <p className="mt-2 text-xs text-amber-300">{scope.warning}</p> : null}
    </div>
  )
}

export default function HistoricalSetupMatchPanel({ symbol, side }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!symbol) return undefined
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setError('')
      try {
        const payload = await api.historicalSetupMatch(symbol, {
          side,
          interval: '15m',
          period: '3y',
          ensure_backfill: true,
          include_contracts: true,
        })
        if (!cancelled) setData(payload)
      } catch (e) {
        if (!cancelled) setError(e.message || 'Historical setup match unavailable')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [symbol, side])

  const topMatch = useMemo(() => (data?.matches || [])[0], [data])
  const indicators = data?.current_feature_vector?.matching_indicators || []
  const missing = data?.current_feature_vector?.missing_or_unconfirmed || []
  const contract = data?.contract_selection?.best_contract

  return (
    <div className="rounded border border-slate-700/80 bg-panel2 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-slate-100">Historical Setup Match</p>
          <p className="text-xs text-slate-400">Deterministic 15-minute pattern matching, stored SQL first.</p>
        </div>
        {data?.setup_state ? (
          <span className={`rounded border px-2 py-0.5 text-xs font-bold ${confidenceTone(data?.estimated_probability?.confidence)}`}>
            {data.setup_state}
          </span>
        ) : null}
      </div>

      {loading && <p className="mt-3 text-sm text-slate-300">Loading historical match...</p>}
      {error && <p className="mt-3 text-sm text-red-300">{error}</p>}

      {data && (
        <div className="mt-3 space-y-3">
          <div className="grid gap-3 lg:grid-cols-2">
            <div>
              <p className="text-xs text-slate-400">Setup</p>
              <p className="font-semibold text-slate-100">{data.setup_name || 'No setup identified'} | {data.direction}</p>
              <p className="mt-1 text-xs text-slate-400">
                Timestamp: {data.timestamp ? formatCentralTime(data.timestamp) : '-'} | Version {data.feature_version}
              </p>
            </div>
            <div>
              <p className="text-xs text-slate-400">Probability language</p>
              <p className="text-sm text-slate-200">{data.estimated_probability?.language || data.message || 'No probability available.'}</p>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <ScopeStats title="Same-symbol history" scope={data.same_symbol} />
            <ScopeStats title="Watchlist-wide history" scope={data.cross_symbol} />
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <div className="rounded border border-slate-700/80 bg-slate-950/30 p-3">
              <p className="font-semibold text-slate-100">Current confirmation</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {indicators.length ? indicators.map((item) => (
                  <span key={item} className="rounded border border-emerald-800/60 bg-emerald-950/30 px-2 py-0.5 text-xs text-emerald-200">{item}</span>
                )) : <span className="text-xs text-slate-400">No confirmed indicators yet.</span>}
              </div>
              {missing.length ? (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {missing.map((item) => (
                    <span key={item} className="rounded border border-amber-800/60 bg-amber-950/30 px-2 py-0.5 text-xs text-amber-200">{item}</span>
                  ))}
                </div>
              ) : null}
            </div>

            <div className="rounded border border-slate-700/80 bg-slate-950/30 p-3">
              <p className="font-semibold text-slate-100">Conditions</p>
              <p className="mt-2 text-xs text-slate-300">Confirm: {data.confirmation_condition?.condition || '-'}</p>
              <p className="mt-1 text-xs text-slate-300">Invalidate: {data.invalidation_condition?.condition || '-'}</p>
              {topMatch ? (
                <p className="mt-2 text-xs text-slate-400">
                  Closest match: {topMatch.symbol} at {formatCentralTime(topMatch.timestamp)} | Similarity {topMatch.similarity_score}%
                </p>
              ) : null}
            </div>
          </div>

          <div className="rounded border border-slate-700/80 bg-slate-950/30 p-3">
            <p className="font-semibold text-slate-100">Contract expression</p>
            {contract ? (
              <p className="mt-1 text-sm text-slate-200">
                Best acceptable: {contract.contract} | {contract.expiration} | Strike {contract.strike} | Quality {contract.quality_score}/100.
              </p>
            ) : (
              <p className="mt-1 text-sm text-slate-300">{data.contract_selection?.message || 'No contract review available.'}</p>
            )}
            <p className="mt-1 text-xs text-slate-400">
              Status: {data.contract_selection?.status || '-'} | Provider: {data.contract_selection?.provider || '-'}
            </p>
          </div>

          {data.warnings?.length ? (
            <div className="space-y-1 text-xs text-amber-300">
              {data.warnings.map((warning) => <p key={warning}>Warning: {warning}</p>)}
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}
