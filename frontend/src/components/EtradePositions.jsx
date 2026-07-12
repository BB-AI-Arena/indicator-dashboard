import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { formatCentralTime, formatEasternTime } from '../utils/time'
import { buildOptionPresentation, buildPositionDecision, formatMoney as formatOptionMoney } from '../utils/optionAnalytics'
import MoneyFlowPanel from './MoneyFlowPanel'
import { buildMoneyFlow } from '../utils/moneyFlow'
import PositionChartPanel from './PositionChartPanel'
import NewsCatalystPanel from './NewsCatalystPanel'
import ExitManagementPanel from './ExitManagementPanel'

function money(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `$${n.toFixed(digits)}` : '-'
}

function pct(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(digits)}%` : '-'
}

function TextList({ items }) {
  const rows = Array.isArray(items) ? items.filter(Boolean) : []
  if (!rows.length) return <p>-</p>
  return (
    <ul className="space-y-1">
      {rows.map((item, idx) => <li key={`${idx}-${item}`}>{item}</li>)}
    </ul>
  )
}

function actionTone(action) {
  const value = String(action || '').toUpperCase()
  if (['CLOSE', 'REDUCE'].includes(value)) return 'border-red-700/60 bg-red-900/30 text-red-200'
  if (['ROLL', 'TRIM'].includes(value)) return 'border-amber-700/60 bg-amber-900/30 text-amber-200'
  if (value === 'WATCH') return 'border-sky-700/60 bg-sky-900/30 text-sky-200'
  if (value === 'HOLD') return 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
  return 'border-slate-700 bg-panel2 text-slate-200'
}

function riskTone(source) {
  const value = String(source || '').toLowerCase()
  if (value === 'openai') return 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
  if (value === 'deterministic') return 'border-sky-700/60 bg-sky-900/30 text-sky-200'
  return 'border-amber-700/60 bg-amber-900/30 text-amber-200'
}

function PositionCard({ position, marketSession, currentUser }) {
  const advice = position.advice || {}
  const session = marketSession || position.market_session || null
  const quote = position.underlying_quote || {
    price: position.underlying_price,
    provider: position.underlying_quote_source || position.source || 'etrade',
    source: position.underlying_quote_source || position.source || 'etrade',
    timestamp: position.quote_timestamp,
    quote_type: position.quote_type,
  }
  const quoteUpdated = quote.timestamp || position.quote_timestamp || position.underlying_quote?.timestamp
  const actionable = session == null ? true : Boolean(session?.actionable_live_quotes)
  const presentation = buildOptionPresentation({
    contract: {
      contract_symbol: position.display_symbol,
      type: position.contract_type,
      strike: position.strike,
      expiration: position.expiration,
      bid: position.bid,
      ask: position.ask,
      last: position.last,
      spread_percentage: position.spread_pct,
      volume: position.volume,
      open_interest: position.open_interest,
      quote_type: position.quote_type,
      quote_stale: position.quote_stale,
      underlying_price: position.underlying_price,
      delta: position.delta,
      gamma: position.gamma,
      theta: position.theta,
      vega: position.vega,
      implied_volatility: position.implied_volatility,
      moneyness: position.moneyness,
      premium: position.premium,
    },
    marketSession: session,
    pricingTimestamp: position.quote_timestamp || session?.current_eastern_timestamp || new Date(),
    side: position.direction,
  })
  const decision = buildPositionDecision({ position, marketSession: session })
  const moneyFlow = buildMoneyFlow({
    symbol: position.symbol,
    side: position.direction,
    marketSession: session,
    position,
    moneyFlow: position.money_flow,
  })
  const historicalContext = position.historical_context || {}
  const historicalIntervals = historicalContext.intervals || {}
  const historicalChart = position.historical_chart || historicalContext.chart || null
  const newsCatalyst = position.news_catalyst || null
  const liveWarnings = actionable ? presentation.live_execution_warnings : []
  const structuralWarnings = Array.from(new Set([
    ...presentation.structural_warnings,
    ...(Array.isArray(advice.risk_notes) ? advice.risk_notes : []),
  ].filter(Boolean)))
  const positionRiskWarnings = Array.from(new Set([
    ...presentation.position_risk_warnings,
    ...(Array.isArray(advice.close_if) ? advice.close_if : []),
    ...(Array.isArray(advice.roll_if) ? advice.roll_if : []),
  ].filter(Boolean)))
  const quoteSessionLabel = session == null ? 'Loading' : (session?.actionable_live_quotes ? 'Live' : 'Previous session')

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-lg font-semibold text-slate-100">{position.display_symbol || position.symbol || '-'}</p>
          <p className="text-xs text-slate-400">
            {position.account_name || 'Account'}{position.account_id_suffix ? ` • ${position.account_id_suffix}` : ''}
            {position.strategy ? ` • ${position.strategy}` : ''}
          </p>
        </div>
        <div className={`badge border ${actionTone(decision.status || advice.action)}`}>
          {decision.status || advice.action || 'WATCH'}
        </div>
      </div>

      <div className="mt-2 rounded border border-slate-700 bg-panel2 p-3 text-xs text-slate-300">
        Session: <span className="font-semibold text-slate-100">{session?.session_state || 'UNKNOWN'}</span> | Quote label: <span className="font-semibold text-slate-100">{quoteSessionLabel}</span>
        {session?.session_note ? <div className="mt-1 text-amber-200">{session.session_note}</div> : null}
      </div>

      <ExitManagementPanel position={position} real />

      <div className="mt-3">
        <PositionChartPanel
          title="15-Minute Position Chart"
          indicatorData={historicalChart}
          marketSession={session}
          position={position}
          moneyFlow={moneyFlow}
          optionPresentation={presentation}
          newsCatalyst={newsCatalyst}
          currentUser={currentUser}
          side={position.direction}
        />
      </div>

      <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div>Direction: <span className="font-semibold">{position.direction || '-'}</span></div>
        <div>Type: <span className="font-semibold">{presentation.contract_type || '-'}</span></div>
        <div>Qty: <span className="font-semibold">{position.quantity ?? '-'}</span></div>
        <div>Signed Qty: <span className="font-semibold">{position.signed_quantity ?? '-'}</span></div>
        <div>Expiration: <span className="font-semibold">{position.expiration || '-'}</span></div>
        <div>DTE: <span className="font-semibold">{presentation.dte ?? '-'}</span></div>
        <div>Strike: <span className="font-semibold">{money(position.strike)}</span></div>
        <div>Moneyness: <span className="font-semibold">{position.moneyness || '-'}</span></div>
        <div>Intrinsic / Extrinsic: <span className="font-semibold">{presentation.intrinsic_or_extrinsic}</span></div>
        <div>ITM / ATM / OTM: <span className="font-semibold">{presentation.in_the_money_label}</span></div>
        <div>Bid / Ask: <span className="font-semibold">{money(position.bid)} / {money(position.ask)}</span></div>
        <div>Last / Premium: <span className="font-semibold">{money(position.last)} / {money(position.premium)}</span></div>
        <div>Spread: <span className="font-semibold">{pct(position.spread_pct)}</span></div>
        <div>Volume / OI: <span className="font-semibold">{position.volume ?? '-'} / {position.open_interest ?? '-'}</span></div>
        <div>Market Value: <span className="font-semibold">{money(position.market_value)}</span></div>
        <div>Cost Basis: <span className="font-semibold">{money(position.cost_basis)}</span></div>
        <div>Unrealized P/L: <span className={`font-semibold ${Number(position.unrealized_pnl) >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>{money(position.unrealized_pnl)} ({pct(position.unrealized_pnl_pct)})</span></div>
        <div>Day Gain: <span className="font-semibold">{money(position.day_gain)} ({pct(position.day_gain_pct)})</span></div>
        <div>Quote Type: <span className="font-semibold">{position.quote_type || '-'}</span></div>
        <div>Quote Time: <span className="font-semibold">{formatEasternTime(position.quote_timestamp, { seconds: false })}</span></div>
        <div>Underlying: <span className="font-semibold">{position.symbol || '-'}</span></div>
        <div>Underlying Price: <span className="font-semibold">{money(quote.price ?? position.underlying_price)}</span></div>
        <div>Underlying Updated: <span className="font-semibold">{formatEasternTime(quoteUpdated, { seconds: false })}</span></div>
        <div>Quote Source: <span className="font-semibold">{quote.provider || position.underlying_quote_source || position.source || '-'}</span></div>
        <div>Current Premium Basis: <span className="font-semibold">{presentation.current_premium_basis}</span></div>
      </div>

      <div className="mt-3 rounded border border-slate-700 bg-panel2 p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-semibold text-slate-100">Historical Data Loaded</p>
          <div className="text-xs text-slate-400">
            Selected interval: <span className="font-semibold text-slate-200">{historicalContext.selected_interval || '-'}</span>
            {historicalContext.selected_provider ? ` • Provider: ${historicalContext.selected_provider}` : ''}
            {historicalContext.selected_bars_loaded != null ? ` • Bars: ${historicalContext.selected_bars_loaded}` : ''}
          </div>
        </div>
        {historicalContext.selected_first_timestamp || historicalContext.selected_last_timestamp ? (
          <p className="mt-1 text-xs text-slate-400">
            Range: {formatEasternTime(historicalContext.selected_first_timestamp, { seconds: false })} to {formatEasternTime(historicalContext.selected_last_timestamp, { seconds: false })}
          </p>
        ) : null}
        <div className="mt-3 grid gap-2 md:grid-cols-3">
          {['5m', '15m', '1d'].map((interval) => {
            const profile = historicalIntervals[interval]
            if (!profile) return null
            const selected = historicalContext.selected_interval === interval
            return (
              <div key={interval} className={`rounded border p-2 text-xs ${selected ? 'border-accent bg-slate-900/60 text-slate-100' : 'border-slate-700 bg-slate-900/30 text-slate-300'}`}>
                <div className="flex items-center justify-between gap-2">
                  <span className="font-semibold uppercase">{interval}</span>
                  <span className="text-[10px] uppercase text-slate-400">{profile.status || 'unknown'}</span>
                </div>
                <p className="mt-1">Provider: <span className="font-semibold">{profile.provider || '-'}</span></p>
                <p>Requested: <span className="font-semibold">{profile.requested_period || '-'}</span></p>
                <p>Bars loaded: <span className="font-semibold">{profile.bars_loaded ?? '-'}</span></p>
                <p>First / last: <span className="font-semibold">{profile.first_timestamp ? formatEasternTime(profile.first_timestamp, { seconds: false }) : '-'}</span> / <span className="font-semibold">{profile.last_timestamp ? formatEasternTime(profile.last_timestamp, { seconds: false }) : '-'}</span></p>
                {profile.warning ? <p className="mt-1 text-amber-300">{profile.warning}</p> : null}
              </div>
            )
          })}
        </div>
      </div>

      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
          <p className="font-semibold text-slate-100">Greeks</p>
          <p className="mt-1">Directional sensitivity: {presentation.labels.directional_sensitivity}</p>
          <p>{presentation.greeks.delta.label === '-' ? 'Delta unavailable.' : `Delta ${presentation.greeks.delta.label} - ${presentation.greeks.delta.description}`}</p>
          <p>{presentation.greeks.gamma.label === '-' ? 'Gamma unavailable.' : `Gamma ${presentation.greeks.gamma.label} - ${presentation.greeks.gamma.description}`}</p>
          <p>{presentation.greeks.theta.label === '-' ? 'Theta unavailable.' : `Theta ${presentation.greeks.theta.label} - ${presentation.greeks.theta.description}`}</p>
          <p>{presentation.greeks.vega.label === '-' ? 'Vega unavailable.' : `Vega ${presentation.greeks.vega.label} - ${presentation.greeks.vega.description}`}</p>
          <p>Gamma risk: {presentation.labels.gamma_risk}</p>
          <p>Time-decay risk: {presentation.labels.time_decay_risk}</p>
          <p>IV sensitivity: {presentation.labels.iv_sensitivity}</p>
          <p>Liquidity: {presentation.labels.liquidity}</p>
          <p>Contract quality: {presentation.labels.contract_quality}</p>
          {Number.isFinite(presentation.greeks.probability_itm) && <p>Estimated probability ITM: {presentation.greeks.probability_itm.toFixed(1)}%</p>}
        </div>

        <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
          <p className="font-semibold text-slate-100">Position Decision</p>
          <p className="mt-1">Status: {decision.status}</p>
          <p>Current position value: {money(decision.current_position_value)}</p>
          <p>Average cost: {money(decision.average_cost)}</p>
          <p>Unrealized P/L: {money(decision.unrealized_pnl)} ({pct(decision.return_pct)})</p>
          <p>Return percentage: {pct(decision.return_pct)}</p>
          <p>Underlying entry price: {position.entry_underlying_price ? money(position.entry_underlying_price) : '-'}</p>
          <p>Current underlying price: {money(decision.current_underlying_price ?? quote.price ?? position.underlying_price)}</p>
          <p>Option breakeven at expiration: {decision.breakeven_at_expiration ? money(decision.breakeven_at_expiration) : '-'}</p>
          <p>Days remaining: {decision.days_remaining ?? '-'}</p>
          <p>Theta cost per day: {decision.theta_cost_per_day ? money(decision.theta_cost_per_day) : '-'}</p>
          <p>Underlying move to offset one day of theta: {decision.underlying_move_to_offset_theta ? money(decision.underlying_move_to_offset_theta) : '-'}</p>
          <p>Relying on time/IV: {decision.relying_on_time_or_iv ? 'Yes' : 'No'}</p>
          <p>Thesis valid: {decision.thesis_valid ? 'Yes' : 'No'}</p>
          <div className="mt-2 grid gap-2">
            <div className="rounded border border-slate-700 bg-slate-900/40 p-2">
              <p className="text-xs uppercase text-slate-400">Reasons to continue holding</p>
              <TextList items={decision.reasons_to_continue_holding} />
            </div>
            <div className="rounded border border-slate-700 bg-slate-900/40 p-2">
              <p className="text-xs uppercase text-slate-400">Reasons to reduce or close</p>
              <TextList items={decision.reasons_to_reduce_or_close} />
            </div>
            <div className="rounded border border-slate-700 bg-slate-900/40 p-2">
              <p className="text-xs uppercase text-slate-400">Exact conditions</p>
              <TextList items={decision.exact_conditions} />
            </div>
          </div>
        </div>

        <MoneyFlowPanel moneyFlow={moneyFlow} title="Money Flow" compact={false} />
        <NewsCatalystPanel newsCatalyst={newsCatalyst} />

        <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
          <p className="font-semibold text-slate-100">Scenario Estimates</p>
          <div className="mt-2 grid gap-2">
            {presentation.scenarios.map((scenario) => (
              <div key={scenario.label} className="rounded border border-slate-700 bg-slate-900/40 p-2">
                <p className="text-xs uppercase text-slate-400">{scenario.label}</p>
                <p>Estimated option price: {scenario.estimated_option_price === null ? '-' : formatOptionMoney(scenario.estimated_option_price)}</p>
                <p>Estimated change: {scenario.estimated_pct_change === null ? '-' : `${scenario.estimated_pct_change.toFixed(2)}%`}</p>
                <p className="text-xs text-slate-400">Underlying assumption: {scenario.underlying_price_assumption === null ? '-' : formatOptionMoney(scenario.underlying_price_assumption)}</p>
                <p className="text-xs text-slate-400">Time assumption: {scenario.time_assumption}</p>
                <p className="text-xs text-slate-400">IV assumption: {scenario.implied_volatility_assumption}</p>
                <p className="text-xs text-slate-400">Pricing timestamp: {formatEasternTime(scenario.pricing_timestamp, { seconds: false })}</p>
                <p className="text-xs text-slate-400">Confidence: {scenario.confidence}</p>
                <p className="text-xs text-slate-400">Model: {scenario.model}</p>
              </div>
            ))}
          </div>
        </div>
      </div>

      {(!actionable || liveWarnings.length > 0 || structuralWarnings.length > 0 || positionRiskWarnings.length > 0) && (
        <div className="mt-3 rounded border border-slate-700 bg-panel2 p-3 text-sm">
          <p className="font-semibold text-slate-100">Warnings</p>
          {!actionable && (
            <div className="mt-2 rounded border border-amber-800/60 bg-amber-900/20 p-3 text-amber-200">
              Market closed - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open.
            </div>
          )}
          {liveWarnings.length > 0 && (
            <div className="mt-2 text-amber-300">
              <p className="text-xs uppercase text-amber-200">Live execution</p>
              <TextList items={liveWarnings} />
            </div>
          )}
          {structuralWarnings.length > 0 && (
            <div className="mt-2 text-sky-300">
              <p className="text-xs uppercase text-sky-200">Structural contract</p>
              <TextList items={structuralWarnings} />
            </div>
          )}
          {positionRiskWarnings.length > 0 && (
            <div className="mt-2 text-red-300">
              <p className="text-xs uppercase text-red-200">Position risk</p>
              <TextList items={positionRiskWarnings} />
            </div>
          )}
        </div>
      )}

      <div className="mt-3 rounded border border-slate-700 bg-panel2 p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-semibold text-slate-100">AI Advice</p>
          <span className={`badge border ${riskTone(advice.source)}`}>{advice.source || 'deterministic'}</span>
        </div>
        <p className="mt-1 text-slate-200">{advice.summary || 'No advice available.'}</p>
        <div className="mt-3 grid gap-3 lg:grid-cols-3">
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Watch for</p>
            <TextList items={advice.watch_for} />
          </div>
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Close if</p>
            <TextList items={advice.close_if} />
          </div>
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Roll if</p>
            <TextList items={advice.roll_if} />
          </div>
        </div>
      </div>
    </div>
  )
}

export default function EtradePositions({ currentUser, marketSession }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const hasLoadedOnce = useRef(false)

  const load = async (refresh = false) => {
    if (!hasLoadedOnce.current || refresh) setLoading(true)
    if (refresh) setRefreshing(true)
    setError('')
    try {
      const res = await api.etradeOptionPositions(refresh)
      setData(res)
    } catch (e) {
      setError(e.message)
    } finally {
      hasLoadedOnce.current = true
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => {
    if (currentUser?.role !== 'admin') return
    load(false)
    const id = setInterval(() => load(false), 60000)
    return () => clearInterval(id)
  }, [currentUser?.username])

  const summary = data?.summary || {}
  const portfolioSummary = summary?.portfolio_summary || data?.ai?.summary || {}
  const aiBlockingReason = summary?.ai_blocking_reason || data?.ai?.blocking_reason || ''
  const session = marketSession || data?.market_session || null
  const snapshotStatus = String(data?.status || '').toLowerCase()
  const snapshotRefreshing = snapshotStatus === 'loading' || snapshotStatus === 'refreshing'

  const refresh = async () => {
    await load(true)
  }

  if (loading && !data) {
    return <div className="card p-4">Loading E*TRADE positions...</div>
  }

  return (
    <div className="space-y-4">
      <div className="card p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
          <h2 className="text-xl font-semibold">REAL E*TRADE Positions</h2>
            <p className="mt-1 text-sm text-slate-400">
              Admin view of open option positions, contract details, current quote context, and management advice.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" onClick={refresh} disabled={refreshing}>
              {refreshing ? 'Refreshing...' : 'Refresh'}
            </button>
          </div>
        </div>

        {data?.message && <p className="mt-3 text-sm text-amber-300">{data.message}</p>}
        {error && <p className="mt-3 text-sm text-red-300">{error}</p>}
        {snapshotRefreshing && (
          <p className="mt-3 rounded border border-sky-800/60 bg-sky-950/40 p-3 text-sm text-sky-200">
            E*TRADE positions snapshot is building in the background. The page will fill in when the refresh completes.
          </p>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
        <div className="card p-4">
          <p className="text-xs uppercase text-slate-400">Accounts</p>
          <p className="mt-1 text-2xl font-bold">{summary.account_count ?? 0}</p>
        </div>
        <div className="card p-4">
          <p className="text-xs uppercase text-slate-400">Open Option Positions</p>
          <p className="mt-1 text-2xl font-bold">{summary.position_count ?? 0}</p>
        </div>
        <div className="card p-4">
          <p className="text-xs uppercase text-slate-400">Unrealized P/L</p>
          <p className={`mt-1 text-2xl font-bold ${Number(summary.total_unrealized_pnl) >= 0 ? 'text-emerald-300' : 'text-red-300'}`}>
            {money(summary.total_unrealized_pnl)}
          </p>
        </div>
        <div className="card p-4">
          <p className="text-xs uppercase text-slate-400">Market Value</p>
          <p className="mt-1 text-2xl font-bold">{money(summary.total_market_value)}</p>
        </div>
        <div className="card p-4">
          <p className="text-xs uppercase text-slate-400">Real Account Equity</p>
          <p className="mt-1 text-2xl font-bold">{money(summary.real_account_equity)}</p>
        </div>
        <div className="card p-4">
          <p className="text-xs uppercase text-slate-400">Real Cash / Buying Power</p>
          <p className="mt-1 text-lg font-bold">{money(summary.real_cash_balance)} / {money(summary.real_buying_power)}</p>
        </div>
      </div>

      <div className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-slate-100">AI Portfolio Summary</p>
            <p className="text-xs text-slate-400">
              AI status: <span className="font-semibold text-slate-200">{summary.ai_status || 'unavailable'}</span>
              {summary.ai_model ? ` | Model: ${summary.ai_model}` : ''}
            </p>
          </div>
          <div className="text-xs text-slate-400">
            Updated: <span className="font-semibold text-slate-200">{formatCentralTime(data?.generated_at || null)}</span>
          </div>
        </div>
        <p className="mt-2 text-sm text-slate-200">{portfolioSummary.headline || 'No AI summary available yet.'}</p>
        {Array.isArray(portfolioSummary.priority_actions) && portfolioSummary.priority_actions.length > 0 && (
          <div className="mt-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Priority actions</p>
            <TextList items={portfolioSummary.priority_actions} />
          </div>
        )}
        {aiBlockingReason && (
          <p className="mt-3 rounded border border-amber-800/60 bg-amber-900/20 p-3 text-sm text-amber-200">
            AI note: {aiBlockingReason} Deterministic position guidance remains active below.
          </p>
        )}
      </div>

      {session?.session_state && (
        <div className="card p-4 text-sm text-slate-300">
          Session state: <span className="font-semibold text-slate-100">{session.session_state}</span> | Quote label: <span className="font-semibold text-slate-100">{session.actionable_live_quotes ? 'Live' : 'Previous session'}</span>
          {session.session_note ? <div className="mt-1 text-amber-200">{session.session_note}</div> : null}
        </div>
      )}

      {summary.quote_sources?.length ? (
        <div className="card p-4 text-sm text-slate-300">
          Quote sources used for underlying context: {summary.quote_sources.join(', ')}
        </div>
      ) : null}

      {summary.historical_sources?.length ? (
        <div className="card p-4 text-sm text-slate-300">
          Historical sources loaded for open positions: {summary.historical_sources.join(', ')}
          {summary.historical_bars_loaded != null ? ` | Bars loaded: ${summary.historical_bars_loaded}` : ''}
        </div>
      ) : null}

      {!data?.accounts?.length ? (
        snapshotRefreshing ? (
          <div className="card p-4 text-sm text-slate-300">
            E*TRADE positions are still loading from the broker. If the refresh is slow, the backend is rebuilding the snapshot and will populate this view automatically.
            {data?.refresh_state?.started_at ? (
              <div className="mt-2 text-xs text-slate-500">Refresh started: {formatCentralTime(data.refresh_state.started_at)}</div>
            ) : null}
            {data?.refresh_state?.last_error ? (
              <div className="mt-2 text-xs text-amber-300">Last refresh error: {data.refresh_state.last_error}</div>
            ) : null}
          </div>
        ) : (
        <div className="card p-4 text-sm text-slate-300">
          No open option positions were returned for the connected E*TRADE accounts.
        </div>
        )
      ) : (
        <div className="space-y-4">
          {data.accounts.map((account) => (
            <section key={`${account.account_id_key || account.account_id_suffix || account.account_name}`} className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h3 className="text-lg font-semibold text-slate-100">{account.account_name || 'E*TRADE Account'}</h3>
                  <p className="text-xs text-slate-400">
                    {account.account_type || '-'}{account.account_id_suffix ? ` • ${account.account_id_suffix}` : ''}
                    {account.position_count ? ` • ${account.position_count} position${account.position_count === 1 ? '' : 's'}` : ''}
                  </p>
                </div>
                <div className="text-xs text-slate-400">
                  Updated: <span className="font-semibold text-slate-200">{formatCentralTime(account.positions?.[0]?.quote_timestamp || data?.generated_at || null)}</span>
                </div>
              </div>
              <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-2 items-start">
                  {(account.positions || []).map((position) => (
                    <PositionCard key={position.position_id} position={position} marketSession={session} currentUser={currentUser} />
                  ))}
                </div>
              </section>
          ))}
        </div>
      )}
    </div>
  )
}
