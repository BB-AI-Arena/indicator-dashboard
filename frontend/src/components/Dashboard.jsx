import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatCentralTime } from '../utils/time'
import RecommendationPerformance from './RecommendationPerformance'

function money(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `$${Number(value).toFixed(digits)}`
}

function pct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${(Number(value) * 100).toFixed(1)}%`
}

function pctRaw(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${Number(value).toFixed(2)}%`
}

function toneForDirection(direction) {
  return direction === 'SHORT'
    ? 'border-red-900/60 bg-red-950/20 text-red-100'
    : 'border-emerald-900/60 bg-emerald-950/20 text-emerald-100'
}

function statusTone(status) {
  const text = String(status || '').toUpperCase()
  if (text.includes('READY') || text === 'TRIGGERED') return 'border-emerald-700/70 bg-emerald-950/40 text-emerald-200'
  if (text === 'NEXT-SESSION WATCH' || text === 'WAITING') return 'border-sky-700/70 bg-sky-950/40 text-sky-200'
  if (['BUILDING', 'PARTIAL', 'ANALYSIS_PENDING', 'STALE', 'DATA REFRESH REQUIRED'].includes(text)) return 'border-amber-700/70 bg-amber-950/30 text-amber-200'
  if (['BLOCKED', 'ERROR', 'NOT_STARTED'].includes(text)) return 'border-red-700/70 bg-red-950/30 text-red-200'
  return 'border-slate-700 bg-slate-900/60 text-slate-200'
}

function confidenceText(match) {
  const sample = Number(match?.sample_size || 0)
  const successes = Number(match?.successes || 0)
  const rate = match?.target_before_invalidation_rate
  if (!sample || rate === null || rate === undefined) return 'No probability displayed; sample is insufficient.'
  return `${successes} of ${sample} reached target before invalidation (${pct(rate)}), ${String(match?.confidence || 'low').toLowerCase()} confidence.`
}

function targetLine(target, index) {
  if (!target) return '-'
  return `T${index}: ${money(target.price)} (${target.source || 'structure'}, likelihood ${pct(target.likelihood_before_invalidation)}, n=${target.sample_size || 0})`
}

function ContractSummary({ contract }) {
  if (!contract) return <span>-</span>
  if (contract.status === 'PENDING_VALIDATION') {
    return <span>{contract.type} pending live option-chain validation</span>
  }
  return (
    <span>
      {contract.contract || contract.symbol || '-'}
      {contract.expiration ? ` | ${contract.expiration}` : ''}
      {contract.strike ? ` | ${money(contract.strike)}` : ''}
      {contract.delta ? ` | delta ${Number(contract.delta).toFixed(2)}` : ''}
    </span>
  )
}

function SetupCard({ title, candidate, emptyText, onSelectSymbol }) {
  if (!candidate) {
    return (
      <section className="decision-card decision-card-empty">
        <div className="decision-card-header">
          <p className="decision-kicker">{title}</p>
          <span className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs font-semibold text-slate-300">NO QUALIFIED SETUP</span>
        </div>
        <p className="mt-4 text-sm text-slate-300">{emptyText}</p>
      </section>
    )
  }

  const match = candidate.historical_match || {}
  const targets = candidate.targets || []
  const options = candidate.options_positioning || {}
  const social = candidate.social_narrative || {}
  const visible = candidate.visible_conditions || {}
  const supporting = (candidate.supporting_factors || []).slice(0, 3)
  const conflicts = (candidate.conflicting_factors || []).slice(0, 2)
  return (
    <section className={`decision-card ${toneForDirection(candidate.direction)}`}>
      <div className="decision-card-header">
        <div>
          <p className="decision-kicker">{title}</p>
          <button className="decision-symbol" onClick={() => onSelectSymbol(candidate.ticker)}>
            {candidate.ticker}
          </button>
        </div>
        <span className={`rounded border px-2 py-0.5 text-xs font-semibold ${statusTone(candidate.status)}`}>
          {candidate.status}
        </span>
      </div>

      <div className="mt-3 flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-lg font-semibold text-slate-50">{candidate.direction} | {candidate.setup_name || 'Setup unavailable'}</p>
        <span className="text-xs uppercase text-slate-400">{candidate.next_session_bias || 'INSUFFICIENT DATA'} • {money(candidate.current_or_previous_session_price)}</span>
      </div>
      <p className="mt-2 text-sm leading-6 text-slate-200">{candidate.thesis || 'The setup is waiting for price, VWAP, and volume confirmation.'}</p>

      <div className="decision-primary-grid">
        <div><span>Price structure</span><strong>{visible.price_structure || '-'}</strong></div>
        <div><span>VWAP</span><strong>{visible.vwap || '-'}</strong></div>
        <div><span>Volume</span><strong>{visible.volume || '-'}</strong></div>
        <div><span>Key level</span><strong>{visible.key_level || '-'}</strong></div>
      </div>

      <div className="decision-levels decision-levels-primary">
        <p><span>Entry trigger</span>{candidate.entry_trigger?.condition || '-'}</p>
        <p><span>Invalidation</span>{candidate.invalidation?.condition || '-'}</p>
        <p><span>Targets</span>{targetLine(targets[0], 1)}{targets[1] ? ` • ${targetLine(targets[1], 2)}` : ''}</p>
      </div>

      <div className="decision-contract">
        <span>Preferred contract</span>
        <strong><ContractSummary contract={candidate.preferred_option_contract} /></strong>
        <small>Max entry: {candidate.maximum_acceptable_option_entry ? money(candidate.maximum_acceptable_option_entry) : 'requires live spread validation'}</small>
      </div>

      <div className="decision-compact-meta">
        <span>Historical: {confidenceText(match)}</span>
        <span>Expected value: {pctRaw(candidate.expected_value_estimate)}</span>
        <span>Data: 15m {formatCentralTime(candidate.data_freshness?.latest_15m_candle)}</span>
      </div>

      <div className="decision-evidence-grid decision-evidence-compact">
        <div>
          <p className="decision-subhead">Why it ranks</p>
          {supporting.length ? supporting.map((item) => <p key={item}>+ {item}</p>) : <p>No confirmed primary support yet.</p>}
        </div>
        <div>
          <p className="decision-subhead">Primary conflict</p>
          {conflicts.length ? conflicts.map((item) => <p key={item}>- {item}</p>) : <p>{candidate.primary_risk || 'No primary conflict recorded.'}</p>}
        </div>
      </div>

      <details className="decision-advanced">
        <summary>Advanced analysis</summary>
        <div className="decision-advanced-grid">
          <div><strong>Conviction</strong><span>{candidate.conviction || 'INSUFFICIENT'} • score {candidate.score_status || 'UNAVAILABLE'}{candidate.score == null ? '' : ` (${candidate.score})`}</span></div>
          <div><strong>Options positioning</strong><span>{options.positioning_bias || options.classification || 'Unavailable'}{options.put_call_volume_ratio != null ? ` • P/C volume ${Number(options.put_call_volume_ratio).toFixed(2)}` : ''}</span></div>
          <div><strong>News</strong><span>{candidate.news_impact?.impact_label || 'Unavailable'}</span></div>
          <div><strong>Social narrative</strong><span>{social.classification || 'Unavailable'}{social.mention_velocity ? ` • ${Number(social.mention_velocity).toFixed(1)}x baseline` : ''}</span></div>
          <div><strong>Profile</strong><span>{candidate.profile_state || candidate.profile_status || 'NOT_STARTED'}{candidate.profile_summary?.completeness_percentage == null ? ' • completeness pending' : ` • ${Number(candidate.profile_summary.completeness_percentage).toFixed(0)}% complete`}</span></div>
          <div><strong>Freshness</strong><span>Options {formatCentralTime(candidate.data_freshness?.latest_option_snapshot_at)}</span></div>
        </div>
        <p className="mt-3 text-xs text-slate-400">Detailed indicators, Fibonacci behavior, money flow, Greeks, catalysts, social sentiment, and contract alternatives remain available in the selected ticker’s drill-down.</p>
      </details>
    </section>
  )
}

function CompactCandidate({ candidate, onSelectSymbol }) {
  return (
    <button className="decision-list-row" onClick={() => onSelectSymbol(candidate.ticker)}>
      <div>
        <strong>{candidate.ticker}</strong>
        <span>{candidate.direction} | {candidate.setup_name || 'No setup'} | {candidate.status}</span>
      </div>
      <div className="text-right">
        <strong>{pctRaw(candidate.expected_value_estimate)}</strong>
        <span>{candidate.next_session_bias}</span>
      </div>
    </button>
  )
}

export default function Dashboard({ onSelectSymbol }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    setError('')
    try {
      const payload = await api.decisionDashboard()
      setData(payload)
    } catch (e) {
      setError(e.message || 'Decision dashboard unavailable')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let mounted = true
    const run = async () => {
      if (!mounted) return
      await load()
    }
    run()
    const id = setInterval(run, 60000)
    return () => {
      mounted = false
      clearInterval(id)
    }
  }, [])

  const forming = useMemo(() => data?.forming_setups || [], [data])
  const nextBest = useMemo(() => data?.next_best_setups || [], [data])
  const noTrade = data?.no_trade_conditions || []
  const market = data?.market_state || {}

  return (
    <div className="decision-page">
      <section className="market-state-panel">
        <div>
          <p className="decision-kicker">Market State</p>
          <h2>{market.overall_regime || 'LOADING'}</h2>
          <p>{market.summary || 'Loading stored market state...'}</p>
        </div>
        <div className="market-state-grid">
          <div><span>Session</span><strong>{market.session_state || '-'}</strong></div>
          <div><span>Next open/close</span><strong>{formatCentralTime(market.next_market_open || market.regular_session_close)}</strong></div>
          <div><span>SPY trend</span><strong>{market.spy_trend || '-'}</strong></div>
          <div><span>QQQ trend</span><strong>{market.qqq_trend || '-'}</strong></div>
          <div><span>VIX</span><strong>{market.vix_direction || 'UNAVAILABLE'}</strong></div>
          <div><span>Breadth</span><strong>{market.market_breadth ? `${market.market_breadth.long} long / ${market.market_breadth.short} short` : '-'}</strong></div>
          <div><span>Leading</span><strong>{market.leading_sectors?.join(', ') || '-'}</strong></div>
          <div><span>Lagging</span><strong>{market.lagging_sectors?.join(', ') || '-'}</strong></div>
        </div>
      </section>

      {loading && <div className="card p-4 text-slate-300">Loading decision dashboard from stored profiles...</div>}
      {error && <div className="card p-4 text-bear">{error}</div>}

      <RecommendationPerformance performance={data?.recommendation_performance} />

      <div className="decision-top-grid">
        <SetupCard
          title="Best Long Setup"
          candidate={data?.best_long_setup}
          emptyText="No qualified long setup."
          onSelectSymbol={onSelectSymbol}
        />
        <SetupCard
          title="Best Short Setup"
          candidate={data?.best_short_setup}
          emptyText="No qualified short setup."
          onSelectSymbol={onSelectSymbol}
        />
      </div>

      <div className="decision-secondary-grid">
        <section className="card p-4">
          <div className="decision-section-title">
            <div>
              <p className="decision-kicker">Next Best Setups</p>
              <h3>Ranked by expected value and readiness</h3>
            </div>
          </div>
          <div className="mt-3 space-y-2">
            {nextBest.length ? nextBest.map((candidate) => (
              <CompactCandidate key={`${candidate.ticker}-${candidate.direction}`} candidate={candidate} onSelectSymbol={onSelectSymbol} />
            )) : <p className="text-sm text-slate-400">No additional setups currently meet the minimum standards.</p>}
          </div>
        </section>

        <section className="card p-4">
          <div className="decision-section-title">
            <div>
              <p className="decision-kicker">Forming Setups</p>
              <h3>Close, but missing confirmation or data</h3>
            </div>
          </div>
          <div className="mt-3 space-y-2">
            {forming.length ? forming.slice(0, 6).map((candidate) => (
              <CompactCandidate key={`${candidate.ticker}-${candidate.direction}-${candidate.status}`} candidate={candidate} onSelectSymbol={onSelectSymbol} />
            )) : <p className="text-sm text-slate-400">No forming setups from stored profiles.</p>}
          </div>
        </section>
      </div>

      <section className="no-trade-panel">
        <p className="decision-kicker">No-Trade Conditions</p>
        <h3>Do not force a trade</h3>
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {noTrade.length ? noTrade.map((item) => (
            <div key={item} className="rounded border border-red-900/50 bg-red-950/20 p-3 text-sm text-red-100">{item}</div>
          )) : <div className="rounded border border-emerald-900/50 bg-emerald-950/20 p-3 text-sm text-emerald-100">At least one setup passes the configured hard gates.</div>}
        </div>
        <p className="mt-3 text-xs text-slate-500">{data?.performance_note || 'External provider refresh is never required before rendering this page.'}</p>
      </section>
    </div>
  )
}
