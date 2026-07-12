import { formatCentralTime, formatEasternTime, getCentralHour } from '../utils/time'
import { decisionTone, evaluateTradeSetup, formatPct, labelTone } from '../utils/tradeDecision'
import TradePlanExplanation from './TradePlanExplanation'
import { buildOptionPresentation, formatMoney as formatOptionMoney } from '../utils/optionAnalytics'
import MoneyFlowPanel from './MoneyFlowPanel'
import { buildMoneyFlow } from '../utils/moneyFlow'
import PositionChartPanel from './PositionChartPanel'
import NewsCatalystPanel from './NewsCatalystPanel'

function fmt(value, digits = 2) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '-'
}

function pct(value, digits = 1) {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(digits)}%` : '-'
}

export default function SessionPlayPlan({ scan, indicatorData, contracts, backtest, aiGate, marketSession, currentUser, newsCatalyst }) {
  if (!scan) return null

  const side = (scan.side || 'NEUTRAL').toUpperCase()
  const isDirectional = side === 'LONG' || side === 'SHORT'
  const optionRows = side === 'LONG' ? (contracts?.calls || []) : (contracts?.puts || [])
  const best = optionRows[0]
  const liveSession = marketSession == null ? true : Boolean(marketSession?.actionable_live_quotes)
  const sessionReady = Boolean(marketSession)

  const liveUnderlyingPrice = Number(contracts?.underlying_price)
  const hasLiveUnderlyingPrice = Number.isFinite(liveUnderlyingPrice) && liveUnderlyingPrice > 0
  const price = hasLiveUnderlyingPrice ? liveUnderlyingPrice : null
  const atr = Number(indicatorData?.latest?.atr)
  const atrSafe = Number.isFinite(atr) && atr > 0
    ? atr
    : (hasLiveUnderlyingPrice ? Math.max(price * 0.01, 0.25) : 0.25)

  const stopDistance = hasLiveUnderlyingPrice ? Math.max(atrSafe * 0.6, price * 0.0035) : null
  const target1Distance = hasLiveUnderlyingPrice ? Math.max(atrSafe * 0.5, price * 0.004) : null
  const target2Distance = hasLiveUnderlyingPrice ? Math.max(atrSafe * 1.0, price * 0.008) : null

  const hardStopPrice = isDirectional && hasLiveUnderlyingPrice ? (side === 'LONG' ? price - stopDistance : price + stopDistance) : null
  const target1Price = isDirectional && hasLiveUnderlyingPrice ? (side === 'LONG' ? price + target1Distance : price - target1Distance) : null
  const target2Price = isDirectional && hasLiveUnderlyingPrice ? (side === 'LONG' ? price + target2Distance : price - target2Distance) : null
  const priceTargetText = hasLiveUnderlyingPrice
    ? {
        target1: `$${fmt(target1Price)}`,
        target2: `$${fmt(target2Price)}`,
        hardStop: `$${fmt(hardStopPrice)}`,
      }
    : {
        target1: 'unavailable until the live E*TRADE quote loads',
        target2: 'unavailable until the live E*TRADE quote loads',
        hardStop: 'unavailable until the live E*TRADE quote loads',
      }

  const bid = Number(best?.bid)
  const ask = Number(best?.ask)
  const last = Number(best?.last)
  const entryPremium = Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask > 0 ? (bid + ask) / 2 : last
  const premiumTarget25 = Number.isFinite(entryPremium) ? entryPremium * 1.25 : null
  const premiumTarget40 = Number.isFinite(entryPremium) ? entryPremium * 1.4 : null
  const premiumHardStop = Number.isFinite(entryPremium) ? entryPremium * 0.75 : null
  const trailActivation = Number.isFinite(entryPremium) ? entryPremium * 1.15 : null
  const trailPercent = 10

  const candles = indicatorData?.candles || []
  const lastCandle = candles.length ? candles[candles.length - 1] : null
  const dayKey = lastCandle ? new Date(lastCandle.time * 1000).toISOString().slice(0, 10) : null
  const dayCandles = dayKey
    ? candles.filter((c) => new Date(c.time * 1000).toISOString().slice(0, 10) === dayKey)
    : []
  const opening = dayCandles.length ? dayCandles[0] : null
  const openingRange = dayCandles.slice(0, 3)
  const openingRangeHigh = openingRange.length ? Math.max(...openingRange.map((c) => Number(c.high) || 0)) : null
  const openingRangeLow = openingRange.length ? Math.min(...openingRange.map((c) => Number(c.low) || 0)) : null

  const decision = evaluateTradeSetup({ side, scan, indicatorData, contract: best, contracts, backtest, aiGate, marketSession })
  const approvedFinal = decision.approved
  const optionTitle = approvedFinal ? 'Session Options Play' : 'Candidate Option'
  const optionPresentation = buildOptionPresentation({
    contract: best,
    marketSession,
    pricingTimestamp: contracts?.timestamp || new Date(),
    side,
    referenceLevels: {
      stop: decision.tradeExplanation?.invalidation?.price,
      target1: decision.tradeExplanation?.targets?.target_1,
      target2: decision.tradeExplanation?.targets?.target_2,
      invalidation: decision.tradeExplanation?.invalidation?.price,
    },
  })
  const moneyFlow = buildMoneyFlow({
    symbol: scan?.symbol,
    side,
    marketSession,
    scan,
    indicatorData,
    contracts,
  })
  const samples = Array.isArray(backtest?.sample_trades) ? backtest.sample_trades : []
  const lastSetup = backtest?.last_similar_setup || null

  const recentWindow = samples.slice(-4)
  const recentWins = recentWindow.filter((t) => Boolean(t?.profitable)).length
  const recentCount = recentWindow.length

  const deltaProxy = Number.isFinite(Number(best?.delta)) ? Math.abs(Number(best.delta)) : 0.45
  const lastUnderlyingReturnPct = Number(lastSetup?.day_return_pct)
  const lastEntryPrice = Number(lastSetup?.entry_price)
  const lastDollarMove = Number.isFinite(lastUnderlyingReturnPct) && Number.isFinite(lastEntryPrice)
    ? Math.abs((lastEntryPrice * lastUnderlyingReturnPct) / 100)
    : null
  const estOptionPctFromLast = Number.isFinite(lastDollarMove) && Number.isFinite(entryPremium) && entryPremium > 0
    ? (deltaProxy * lastDollarMove / entryPremium) * 100
    : null

  const lastSetupHour = getCentralHour(lastSetup?.setup_time)
  const ivDisplay = optionPresentation.implied_volatility === null || optionPresentation.implied_volatility === undefined
    ? '-'
    : formatPct(optionPresentation.implied_volatility > 1 ? optionPresentation.implied_volatility : optionPresentation.implied_volatility * 100)
  const timingComment = Number.isFinite(lastSetupHour)
    ? (lastSetupHour >= 13
      ? 'Last similar setup happened after 1:00 PM, so treat late-session entries as lower quality unless momentum expands.'
      : 'Last similar setup happened before 1:00 PM, which is usually cleaner for open-to-close continuation.')
    : 'No reliable timing read from prior setup yet.'

  return (
    <div className="card p-4">
      <h3 className="mb-2 text-lg font-semibold">Session Play Plan (Open to Close)</h3>
      <p className="mb-2 text-xs text-slate-400">
        Systematic plan to attempt premium capture. This is scenario guidance, not guaranteed return.
      </p>
      {sessionReady && !liveSession && (
        <div className="mb-3 rounded border border-amber-700/60 bg-amber-900/20 p-3 text-sm text-amber-200">
          Market closed - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open.
          <div className="mt-1 text-xs text-amber-100">
            {marketSession?.session_note || 'Refresh option chain after 9:30 AM ET.'}
          </div>
        </div>
      )}

      {!isDirectional && (
        <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm text-slate-300">
          No directional edge right now (NEUTRAL). Wait for LONG/SHORT bias before taking a session options play.
        </div>
      )}

      {isDirectional && (
        <div className="space-y-3 text-sm">
          <div className="grid gap-2 md:grid-cols-3">
            {[
              ['Chart Signal', decision.labels.chartSignal, decision.labels.chartSignal !== 'WAIT'],
              ['Historical Edge', decision.labels.historicalEdge, decision.historicalEdge.ok],
              ['Option Liquidity', decision.labels.optionLiquidity, decision.labels.optionLiquidity === 'PASS'],
              ['Data Quality', decision.labels.dataQuality, decision.labels.dataQuality === 'PASS'],
              ['AI Gate', decision.labels.aiGate, decision.labels.aiGate === 'PROCEED', decision.labels.aiGate === 'PENDING'],
              ['Final Decision', decision.labels.finalDecision, approvedFinal],
            ].map(([label, value, ok, pending]) => (
              <div key={label} className={`rounded border px-3 py-2 ${labelTone(Boolean(ok), Boolean(pending))}`}>
                <p className="text-[11px] uppercase">{label}</p>
                <p className={`font-semibold ${label === 'Final Decision' ? decisionTone(value) : ''}`}>{value}</p>
              </div>
            ))}
          </div>

          {!approvedFinal && decision.primaryBlockingReason && (
            <div className="rounded border border-amber-800/60 bg-amber-900/20 p-3 text-amber-200">
              Blocking reason: {decision.primaryBlockingReason}
            </div>
          )}

          <PositionChartPanel
            title="15-Minute Position Chart"
            indicatorData={indicatorData}
            marketSession={marketSession}
            scan={scan}
            contracts={contracts}
            backtest={backtest}
            aiGate={aiGate}
            decision={decision}
            moneyFlow={moneyFlow}
            optionPresentation={optionPresentation}
            currentUser={currentUser}
            side={side}
          />

          <TradePlanExplanation explanation={decision.tradeExplanation} marketSession={marketSession} />
          <MoneyFlowPanel moneyFlow={moneyFlow} title="Money Flow" compact={false} />
          <NewsCatalystPanel newsCatalyst={newsCatalyst} />

          <div className="grid gap-3 md:grid-cols-2">
            <div className="rounded border border-slate-700 bg-panel2 p-3">
              <p className="font-semibold text-slate-100">Underlying Plan ({side})</p>
              <p>Session state: {marketSession?.session_state || 'UNKNOWN'}</p>
              <p>{sessionReady ? (liveSession ? 'Live price' : 'Previous session price') : 'Price' }: {hasLiveUnderlyingPrice ? `$${fmt(price)}` : 'unavailable'}</p>
              <p>Opening print: ${fmt(opening?.open)}</p>
              <p>Opening range high/low: ${fmt(openingRangeHigh)} / ${fmt(openingRangeLow)}</p>
              {marketSession?.next_market_open && <p>Next market open: {formatEasternTime(marketSession.next_market_open, { seconds: false })}</p>}
              <p>Price target 1: {priceTargetText.target1}</p>
              <p>Price target 2: {priceTargetText.target2}</p>
              <p>Hard stop (underlying): {priceTargetText.hardStop}</p>
            </div>

            <div className="rounded border border-slate-700 bg-panel2 p-3">
              <p className="font-semibold text-slate-100">{optionTitle} ({(optionPresentation.contract_type || decision.expectedContractType || '-').toUpperCase()})</p>
              <p>Contract: {optionPresentation.contract_symbol || best?.contract_symbol || '-'}</p>
              <p>Quote session: {sessionReady ? optionPresentation.quote_session_label : 'Loading'} | Expiration: {best?.expiration || '-'}</p>
              <p>Strike: {fmt(best?.strike)} | DTE: {optionPresentation.dte ?? '-'}</p>
              <p>Intrinsic/extrinsic: {optionPresentation.intrinsic_or_extrinsic} | {optionPresentation.in_the_money_label}</p>
              <p>Bid / Ask / Mid / Last: {fmt(best?.bid)} / {fmt(best?.ask)} / {fmt(optionPresentation.current_premium)} / {fmt(best?.last)}</p>
              <p>Spread: {formatPct(optionPresentation.spread_percentage)} | Volume: {optionPresentation.volume ?? '-'} | OI: {optionPresentation.open_interest ?? '-'}</p>
              <p>IV: {ivDisplay} | Liquidity: {optionPresentation.labels.liquidity} | Quality: {optionPresentation.labels.contract_quality}</p>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                <div className="rounded border border-slate-700 bg-slate-900/40 p-2">
                  <p className="text-[11px] uppercase text-slate-400">Greeks</p>
                  <p className="mt-1 text-sm">Directional sensitivity: {optionPresentation.labels.directional_sensitivity}</p>
                  <p className="text-sm">{optionPresentation.greeks.delta.label === '-' ? 'Delta unavailable.' : `Delta ${optionPresentation.greeks.delta.label} - ${optionPresentation.greeks.delta.description}`}</p>
                  <p className="text-sm">{optionPresentation.greeks.gamma.label === '-' ? 'Gamma unavailable.' : `Gamma ${optionPresentation.greeks.gamma.label} - ${optionPresentation.greeks.gamma.description}`}</p>
                  <p className="text-sm">{optionPresentation.greeks.theta.label === '-' ? 'Theta unavailable.' : `Theta ${optionPresentation.greeks.theta.label} - ${optionPresentation.greeks.theta.description}`}</p>
                  <p className="text-sm">{optionPresentation.greeks.vega.label === '-' ? 'Vega unavailable.' : `Vega ${optionPresentation.greeks.vega.label} - ${optionPresentation.greeks.vega.description}`}</p>
                  {Number.isFinite(optionPresentation.greeks.probability_itm) && (
                    <p className="text-sm">Estimated probability ITM: {optionPresentation.greeks.probability_itm.toFixed(1)}%</p>
                  )}
                </div>
                <div className="rounded border border-slate-700 bg-slate-900/40 p-2">
                  <p className="text-[11px] uppercase text-slate-400">Risk labels</p>
                  <p className="mt-1 text-sm">Gamma risk: {optionPresentation.labels.gamma_risk}</p>
                  <p className="text-sm">Time-decay risk: {optionPresentation.labels.time_decay_risk}</p>
                  <p className="text-sm">IV sensitivity: {optionPresentation.labels.iv_sensitivity}</p>
                  <p className="text-sm">Contract quality: {optionPresentation.labels.contract_quality}</p>
                  <p className="text-sm">Current premium basis: {optionPresentation.current_premium_basis}</p>
                </div>
              </div>
              {optionPresentation.live_execution_warnings.length > 0 && liveSession && (
                <div className="mt-2 space-y-1 text-amber-300">
                  {optionPresentation.live_execution_warnings.map((warning) => <p key={warning}>Live warning: {warning}</p>)}
                </div>
              )}
              {optionPresentation.structural_warnings.length > 0 && (
                <div className="mt-2 space-y-1 text-sky-300">
                  {optionPresentation.structural_warnings.map((warning) => <p key={warning}>Structural: {warning}</p>)}
                </div>
              )}
              {optionPresentation.position_risk_warnings.length > 0 && (
                <div className="mt-2 space-y-1 text-red-300">
                  {optionPresentation.position_risk_warnings.map((warning) => <p key={warning}>Position risk: {warning}</p>)}
                </div>
              )}
              <div className="mt-3 rounded border border-slate-700 bg-slate-900/50 p-3">
                <p className="font-semibold text-slate-100">Scenario estimates</p>
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  {optionPresentation.scenarios.map((scenario) => (
                    <div key={scenario.label} className="rounded border border-slate-700 bg-panel2 p-2">
                      <p className="text-xs uppercase text-slate-400">{scenario.label}</p>
                      <p className="text-sm">Estimated option price: {scenario.estimated_option_price === null ? '-' : `$${scenario.estimated_option_price.toFixed(2)}`}</p>
                      <p className="text-sm">Estimated change: {scenario.estimated_pct_change === null ? '-' : `${scenario.estimated_pct_change.toFixed(2)}%`}</p>
                      <p className="text-xs text-slate-400">Underlying assumption: {scenario.underlying_price_assumption === null ? '-' : `$${scenario.underlying_price_assumption.toFixed(2)}`}</p>
                      <p className="text-xs text-slate-400">Time assumption: {scenario.time_assumption}</p>
                      <p className="text-xs text-slate-400">IV assumption: {scenario.implied_volatility_assumption}</p>
                      <p className="text-xs text-slate-400">Confidence: {scenario.confidence}</p>
                    </div>
                  ))}
                </div>
              </div>
              <p className="mt-2 text-xs text-slate-400">Model: scenario values use a Black-Scholes approximation and are directional only, not executable quotes.</p>
            </div>
          </div>

          {aiGate && (
            <div className={`rounded border p-3 ${aiGate.decision === 'PROCEED' ? 'border-emerald-800/60 bg-emerald-900/20 text-emerald-200' : 'border-amber-800/60 bg-amber-900/20 text-amber-200'}`}>
              <p className="font-semibold">AI Trade Gate: {aiGate.decision}</p>
              <p>{aiGate.decision === 'PROCEED' ? aiGate.summary : decision.tradeExplanation?.why_passed_or_failed || aiGate.summary}</p>
            </div>
          )}

          {approvedFinal && (
            <div className="rounded border border-slate-700 bg-panel2 p-3">
              <p className="font-semibold text-slate-100">Execution Rules (AI gate passed)</p>
              <p>1. Entry: only enter when price confirms direction near VWAP/EMA alignment and does not reject opening range.</p>
              <p>2. Take-profit target: option premium ${fmt(premiumTarget25)} (+25%).</p>
              <p>3. Stretch target: option premium ${fmt(premiumTarget40)} (+40%) if trend and volume stay favorable.</p>
              <p>4. Hard stop (option premium): ${fmt(premiumHardStop)} (-25%) {hasLiveUnderlyingPrice ? `or if underlying hits ${fmt(hardStopPrice)}.` : 'and wait for a live E*TRADE quote before setting an underlying stop.'}</p>
              <p>5. Trailing stop: activate once premium reaches ${fmt(trailActivation)} (+15%), trail by {pct(trailPercent, 0)} from peak premium.</p>
              <p>6. Time rule: if no expansion by midday and momentum weakens (MACD flattening / RSI rollover), reduce risk or exit.</p>
            </div>
          )}

          <div className="rounded border border-slate-700 bg-panel2 p-3">
            <p className="font-semibold text-slate-100">Historical Setup Advice</p>
            <p>Sample confidence: {decision.sampleConfidence.label} ({backtest?.occurrences ?? 0} sessions).</p>
            <p>Historical edge: {decision.historicalEdge.label} ({formatPct(backtest?.win_rate_pct)} win rate).</p>
            <p>
              Last similar setup: {formatCentralTime(lastSetup?.setup_time)}.
            </p>
            <p>
              Last similar setup underlying result by close: {pct(lastUnderlyingReturnPct, 2)} (direction-adjusted for {side}).
            </p>
            <p>
              Recent pattern read: {recentCount ? `${recentWins} out of ${recentCount} recent matching setups closed profitably.` : 'Not enough matching samples yet.'}
            </p>
            <p>
              Estimated option impact from last similar underlying move (delta proxy {fmt(deltaProxy, 2)}): {pct(estOptionPctFromLast, 1)}.
            </p>
            <p>{timingComment}</p>
            {!liveSession && (
              <p className="mt-2 text-amber-300">Planning only. Refresh quotes after the next open before changing this from WATCH/WAIT_FOR_CONFIRMATION to a live trade decision.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
