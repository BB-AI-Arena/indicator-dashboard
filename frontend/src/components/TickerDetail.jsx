import { useEffect, useState } from 'react'
import { api } from '../api'
import IndicatorCards from './IndicatorCards'
import MoneyFlowPanel from './MoneyFlowPanel'
import ContractTable from './ContractTable'
import SessionPlayPlan from './SessionPlayPlan'
import { formatCentralTime } from '../utils/time'
import { buildTradeGatePayload } from '../utils/tradeGate'
import { closedAiGate, evaluateHistoricalSupport } from '../utils/tradeDecision'
import { buildMoneyFlow } from '../utils/moneyFlow'

export default function TickerDetail({ symbol, onSymbolChange, marketSession, currentUser }) {
  const [input, setInput] = useState(symbol || 'SPY')
  const [scan, setScan] = useState(null)
  const [quote, setQuote] = useState(null)
  const [indicatorData, setIndicatorData] = useState(null)
  const [ratios, setRatios] = useState(null)
  const [contracts, setContracts] = useState(null)
  const [backtest, setBacktest] = useState(null)
  const [aiGate, setAiGate] = useState(null)
  const [newsCatalyst, setNewsCatalyst] = useState(null)
  const [profile, setProfile] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const moneyFlow = buildMoneyFlow({
    symbol: scan?.symbol || input,
    side: scan?.side,
    marketSession,
    scan,
    indicatorData,
    contracts,
    ratios,
    position: null,
  })

  const normalized = (value) => (value || '').trim().toUpperCase()

  const load = async (sym) => {
    const s = normalized(sym)
    if (!s) return
    setLoading(true)
    setError('')
    setAiGate(null)
    setNewsCatalyst(null)
    setProfile(null)
    onSymbolChange(s)
    try {
      const scanData = await api.scanSymbol(s)
      setScan(scanData)
      const side = (scanData?.side || '').toUpperCase()
      const newsRequest = api.newsCatalystImpact(s, { direction: side, context_type: 'candidate' })

      const [quoteRes, indRes, ratioRes, contractRes, profileRes] = await Promise.allSettled([
        api.quote(s),
        api.indicators(s, '15m', '60d'),
        api.optionsRatios(s),
        api.optionsContracts(s),
        api.tickerProfile(s),
      ])
      const backtestRes = (side === 'LONG' || side === 'SHORT')
        ? await Promise.allSettled([api.backtest(s, side, '15m', '60d', scanData?.score)])
        : [{ status: 'fulfilled', value: null }]

      const errors = []
      let loadedQuote = null
      let loadedRatios = null
      let loadedContracts = null
      let loadedBacktest = null
      if (profileRes.status === 'fulfilled') {
        setProfile(profileRes.value)
      }

      if (quoteRes.status === 'fulfilled') {
        setQuote(quoteRes.value)
        loadedQuote = quoteRes.value
      } else {
        setQuote(null)
        errors.push(`Quote: ${quoteRes.reason?.message || 'Unavailable'}`)
      }

      if (indRes.status === 'fulfilled') {
        setIndicatorData(indRes.value)
      } else {
        setIndicatorData({ symbol: s, candles: [], indicators: [], latest: {} })
        errors.push(`Indicators: ${indRes.reason?.message || 'Unavailable'}`)
      }

      if (ratioRes.status === 'fulfilled') {
        setRatios(ratioRes.value)
        loadedRatios = ratioRes.value
      } else {
        setRatios({ symbol: s, ratios: [], aggregate: {} })
      }

      if (contractRes.status === 'fulfilled') {
        setContracts(contractRes.value)
        loadedContracts = contractRes.value
      } else {
        setContracts({ symbol: s, calls: [], puts: [] })
      }

      newsRequest
        .then((value) => setNewsCatalyst(value))
        .catch(() => setNewsCatalyst(null))

      if (backtestRes[0]?.status === 'fulfilled') {
        setBacktest(backtestRes[0]?.value || null)
        loadedBacktest = backtestRes[0]?.value || null
      } else {
        setBacktest({
          symbol: s,
          side,
          occurrences: 0,
          wins: 0,
          win_rate_pct: null,
          sample_confidence: 'LOW',
          historical_edge: 'UNKNOWN',
          confidence: 'LOW',
          confidence_ok: false,
          last_similar_setup: null,
          sample_trades: [],
          warning: backtestRes[0]?.reason?.message || 'Unavailable',
          warnings: [backtestRes[0]?.reason?.message || 'Unavailable'],
        })
      }

      if (side === 'LONG' || side === 'SHORT') {
        const historicalSupport = evaluateHistoricalSupport(loadedBacktest)
        if (!historicalSupport.ok) {
          setAiGate(closedAiGate(
            `Do not proceed because historical data is the first gate for ${s} ${side}. ${historicalSupport.reason} AI Gate approval is skipped until historical support passes.`,
            ['historical_gate_not_supported'],
          ))
        } else if (loadedContracts) {
          const contract = side === 'LONG' ? loadedContracts.calls?.[0] : loadedContracts.puts?.[0]
          if (contract) {
            try {
              const gate = await api.tradeGate(buildTradeGatePayload({
                symbol: s,
                side,
                scan: scanData,
                contract,
                contracts: loadedContracts,
                ratios: loadedRatios,
                backtest: loadedBacktest,
                quote: loadedQuote,
              }))
              setAiGate(gate)
            } catch (gateError) {
              setAiGate(closedAiGate(
                `Do not proceed because the AI trade gate endpoint failed: ${gateError.message}. The setup cannot receive a final trade approval until the gate returns PROCEED.`,
                ['ai_gate_request_failed'],
              ))
            }
          }
        }
      }

      if (errors.length) {
        setError(errors.join(' | '))
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const addCurrentToWatchlist = async () => {
    const s = normalized(input)
    if (!s) return
    try {
      await api.addWatchlist(s)
      await load(s)
    } catch (e) {
      setError(e.message)
    }
  }

  const removeCurrentFromWatchlist = async () => {
    const s = normalized(input)
    if (!s) return
    try {
      await api.removeWatchlist(s)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    setInput(symbol || 'SPY')
    if (symbol) {
      load(symbol)
    }
  }, [symbol])

  return (
    <div className="space-y-4">
      <div className="card p-4">
        <div className="flex flex-col gap-3 md:flex-row">
          <input className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2" value={input} onChange={(e) => setInput(e.target.value)} />
          <button className="rounded bg-accent px-4 py-2 font-semibold text-slate-900" onClick={addCurrentToWatchlist}>Analyze + Add</button>
          <button className="rounded bg-slate-700 px-4 py-2 font-semibold" onClick={addCurrentToWatchlist}>Add</button>
          <button className="rounded bg-red-900/40 px-4 py-2 font-semibold text-red-200" onClick={removeCurrentFromWatchlist}>Remove</button>
        </div>
      </div>

      {loading && <div className="card p-4">Loading {normalized(input)}...</div>}
      {error && <div className="card p-4 text-bear">{error}</div>}
      {(indicatorData?.warnings?.length || ratios?.warnings?.length || contracts?.warnings?.length) ? (
        <div className="card p-3 text-sm text-amber-300">
          {[...(indicatorData?.warnings || []), ...(ratios?.warnings || []), ...(contracts?.warnings || [])].join(' | ')}
        </div>
      ) : null}

      {scan && (
        <div className="card p-4">
          <h2 className="text-xl font-bold">{scan.symbol} | ${scan.price?.toFixed?.(2)} | <span className={scan.side === 'LONG' ? 'text-bull' : scan.side === 'SHORT' ? 'text-bear' : 'text-neutral'}>{scan.side}</span></h2>
          <p className="text-sm text-slate-300">Score {scan.score}/{scan.max_score} | Grade {scan.grade}</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {(scan.reasons || []).map((r, idx) => <span key={`${r}-${idx}`} className="badge bg-emerald-900/50 text-emerald-300">{r}</span>)}
            {(scan.warnings || []).map((w, idx) => <span key={`${w}-${idx}`} className="badge bg-red-900/50 text-red-300">{w}</span>)}
          </div>
        </div>
      )}

      {quote && (
        <div className="card p-4">
          <h3 className="text-lg font-semibold">Current Quote</h3>
          <div className="mt-2 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
            <div>Provider: <span className="font-semibold">{quote.provider || quote.source || '-'}</span></div>
            <div>Price: <span className="font-semibold">${Number(quote.price || 0).toFixed(2)}</span></div>
            <div>Updated: <span className="font-semibold">{formatCentralTime(quote.timestamp)}</span></div>
            <div>Quote Type: <span className="font-semibold">{quote.quote_type || '-'}</span></div>
          </div>
          {(quote.warning || quote.warnings?.length) && (
            <p className="mt-2 text-sm text-amber-300">{quote.warning || quote.warnings.join(' | ')}</p>
          )}
        </div>
      )}

      {indicatorData && (
        <div className="card p-4 text-sm text-slate-300">
          <div className="flex flex-wrap gap-4">
            <p>Historical source: <span className="font-semibold">{indicatorData.source || indicatorData.provider || '-'}</span></p>
            <p>Last updated: <span className="font-semibold">{formatCentralTime(indicatorData.last_updated || indicatorData.timestamp)}</span></p>
          </div>
        </div>
      )}

      {profile && (
        <div className="card p-4 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="decision-kicker">Profile Readiness</p>
              <h3 className="text-lg font-semibold">{profile.profile_state || profile.profile_status || 'NOT_STARTED'}</h3>
            </div>
            <span className="badge border border-slate-700 bg-slate-900/40 text-slate-200">{profile.completeness_percentage == null ? 'Pending' : `${Number(profile.completeness_percentage).toFixed(0)}% complete`}</span>
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {Object.entries(profile.readiness?.components || {}).slice(0, 24).map(([name, item]) => (
              <div key={name} className="rounded border border-slate-800 bg-panel2 p-2">
                <div className="flex justify-between gap-2"><span className="capitalize text-slate-400">{name.replaceAll('_', ' ')}</span><strong className={item.status === 'COMPLETE' ? 'text-emerald-300' : item.status === 'STALE' ? 'text-amber-300' : item.status === 'INSUFFICIENT_SAMPLE' ? 'text-sky-300' : 'text-red-300'}>{item.status === 'COMPLETE' ? 'Complete' : item.status === 'INSUFFICIENT_SAMPLE' ? 'Insufficient sample' : item.status === 'MISSING_DATA' ? 'Missing' : item.status || 'Pending'}</strong></div>
              </div>
            ))}
          </div>
          {profile.readiness?.next_required_job && <p className="mt-3 text-amber-300">Next required job: {profile.readiness.next_required_job}</p>}
        </div>
      )}

      {profile && (
        <div className="card p-4 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-lg font-semibold">Earnings History</h3>
            <span className="text-xs text-slate-400">
              {profile.stats?.earnings_history?.event_count || 0} reports in the last {profile.stats?.earnings_history?.lookback_days || 365} days
            </span>
          </div>
          <p className="mt-1 text-xs text-slate-400">Beat/miss uses reported versus estimated EPS and revenue when available. Stock reaction uses stored daily candles.</p>
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            {(profile.stats?.earnings_history?.events || []).map((event) => {
              const reaction = event.price_reaction || {}
              const reactionValue = Number(reaction.first_session_return_pct)
              return (
                <div key={`${event.reported_date}-${event.fiscal_date_ending}`} className="rounded border border-slate-700 bg-panel2 p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold text-slate-100">{event.reported_date || 'Date unavailable'}</span>
                    <span className={`badge border ${event.overall_result === 'BEAT' ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200' : event.overall_result === 'MISS' ? 'border-red-700/60 bg-red-900/30 text-red-200' : 'border-slate-700 bg-slate-900/40 text-slate-300'}`}>{event.overall_result || 'UNKNOWN'}</span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">EPS: {event.eps_result || 'UNKNOWN'} • Revenue: {event.revenue_result || 'UNKNOWN'} • {event.report_timing || 'UNKNOWN'}</p>
                  <p className={Number.isFinite(reactionValue) && reactionValue >= 0 ? 'mt-2 text-emerald-300' : 'mt-2 text-red-300'}>
                    Stock reaction: {Number.isFinite(reactionValue) ? `${reactionValue.toFixed(2)}% after the first reaction session` : 'Unavailable'}
                  </p>
                  <p className="text-xs text-slate-400">Gap: {Number.isFinite(Number(reaction.gap_pct)) ? `${Number(reaction.gap_pct).toFixed(2)}%` : '-'} • 3 sessions: {Number.isFinite(Number(reaction['3_session_return_pct'])) ? `${Number(reaction['3_session_return_pct']).toFixed(2)}%` : '-'} • 5 sessions: {Number.isFinite(Number(reaction['5_session_return_pct'])) ? `${Number(reaction['5_session_return_pct']).toFixed(2)}%` : '-'}</p>
                </div>
              )
            })}
          </div>
          {!profile.stats?.earnings_history?.events?.length && <p className="mt-3 text-amber-300">Earnings history is unavailable for this profile. The provider may not have returned records yet.</p>}
        </div>
      )}

      {profile?.stats?.social_history && (
        <div className="card p-4 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-lg font-semibold">Social Narrative</h3>
            <span className="badge border border-slate-700 bg-slate-900/40 text-slate-300">Secondary validator</span>
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <div><span className="text-slate-400">Sentiment</span><p className="font-semibold">{profile.stats.social_history.classification || 'INSUFFICIENT DATA'}</p></div>
            <div><span className="text-slate-400">Score</span><p className="font-semibold">{Number.isFinite(Number(profile.stats.social_history.sentiment_score)) ? Number(profile.stats.social_history.sentiment_score).toFixed(0) : '-'}</p></div>
            <div><span className="text-slate-400">Mentions</span><p className="font-semibold">{profile.stats.social_history.mention_count ?? 0} • {profile.stats.social_history.mention_velocity ? `${Number(profile.stats.social_history.mention_velocity).toFixed(1)}x baseline` : '-'}</p></div>
            <div><span className="text-slate-400">Unique authors</span><p className="font-semibold">{profile.stats.social_history.unique_author_count ?? 0}</p></div>
          </div>
          <div className="mt-3 grid gap-2 md:grid-cols-3">
            <div><span className="text-slate-400">Primary narrative</span><p>{profile.stats.social_history.primary_topics?.[0]?.topic || 'No reliable topic cluster'}</p></div>
            <div><span className="text-slate-400">Source diversity</span><p>{profile.stats.social_history.source_count ?? 0} source(s)</p></div>
            <div><span className="text-slate-400">Spam/hype risk</span><p>{Number.isFinite(Number(profile.stats.social_history.spam_risk_score)) ? `${(Number(profile.stats.social_history.spam_risk_score) * 100).toFixed(0)}%` : 'Unavailable'}</p></div>
          </div>
          <p className="mt-3 text-xs text-slate-400">Price confirmation: {profile.stats.social_history.price_confirmation || 'UNAVAILABLE'} • Options confirmation: {profile.stats.social_history.options_confirmation || 'UNAVAILABLE'} • Confidence: {profile.stats.social_history.sentiment_confidence || 'LOW'}</p>
          {profile.stats.social_history.historical_behavior?.sample_size ? (
            <p className="mt-2 text-xs text-slate-400">
              Historical social-spike sample: {profile.stats.social_history.historical_behavior.sample_size} • positive reaction rate {Number(profile.stats.social_history.historical_behavior.positive_rate * 100).toFixed(1)}% • average reaction {Number(profile.stats.social_history.historical_behavior.average_return_pct).toFixed(2)}%
            </p>
          ) : null}
          {profile.stats.social_history.representative_posts?.length ? (
            <div className="mt-3 border-t border-slate-800 pt-3">
              <p className="text-xs uppercase text-slate-400">Representative public discussions</p>
              <div className="mt-2 space-y-2">
                {profile.stats.social_history.representative_posts.map((post, index) => (
                  <div key={`${post.source}-${post.published_at}-${index}`} className="rounded border border-slate-700 bg-panel2 p-2 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-semibold text-slate-200">{post.source || 'Source'} • {post.stance || 'NEUTRAL'}</span>
                      <span className="text-slate-500">{formatCentralTime(post.published_at)}</span>
                    </div>
                    {post.url ? <a className="mt-1 block text-accent hover:text-emerald-200" href={post.url} target="_blank" rel="noreferrer">{post.title || 'Open source'}</a> : <p className="mt-1 text-slate-300">{post.title || 'No title available'}</p>}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
          {profile.stats.social_history.source_errors?.length ? <p className="mt-2 text-xs text-amber-300">{profile.stats.social_history.source_errors.join(' | ')}</p> : null}
        </div>
      )}

      <IndicatorCards scan={scan} options={ratios} />
      <MoneyFlowPanel moneyFlow={moneyFlow} title="Money Flow" />
      <SessionPlayPlan
        scan={scan}
        indicatorData={indicatorData}
        contracts={contracts}
        backtest={backtest}
        aiGate={aiGate}
        marketSession={marketSession}
        currentUser={currentUser}
        newsCatalyst={newsCatalyst}
      />
      <ContractTable contracts={contracts} side={scan?.side} />
    </div>
  )
}
