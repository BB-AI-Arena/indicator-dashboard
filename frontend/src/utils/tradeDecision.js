const BAD_QUOTE_TYPES = new Set(['CLOSING', 'DELAYED', 'SANDBOX'])
const CONFIRMED_CHART_GRADES = new Set(['TRADE_CANDIDATE', 'HIGH_CONVICTION'])
const APPROVED_FINAL_DECISIONS = new Set(['TRADE_CANDIDATE', 'HIGH_CONVICTION'])

export const FINAL_DECISIONS = [
  'NO_TRADE',
  'WATCH',
  'WAIT_FOR_CONFIRMATION',
  'TRADE_CANDIDATE',
  'HIGH_CONVICTION',
]

export function toNumber(value, fallback = null) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

export function formatPct(value, digits = 2) {
  const n = toNumber(value)
  return n === null ? '-' : `${n.toFixed(digits)}%`
}

export function expectedContractType(side) {
  const normalized = (side || '').toUpperCase()
  if (normalized === 'LONG') return 'CALL'
  if (normalized === 'SHORT') return 'PUT'
  return ''
}

export function isLiveSession(marketSession) {
  return marketSession == null ? true : Boolean(marketSession?.actionable_live_quotes)
}

export function getCandidateContract(side, contracts) {
  const expected = expectedContractType(side)
  if (expected === 'CALL') return contracts?.calls?.[0] || null
  if (expected === 'PUT') return contracts?.puts?.[0] || null
  const combined = [...(contracts?.calls || []), ...(contracts?.puts || [])]
  return combined.sort((a, b) => Number(b?.score || 0) - Number(a?.score || 0))[0] || null
}

export function classifySampleConfidence(occurrences) {
  const n = toNumber(occurrences, 0)
  if (n < 20) return { label: 'LOW', ok: false }
  if (n < 50) return { label: 'MEDIUM', ok: true }
  return { label: 'ENOUGH', ok: true }
}

export function classifyHistoricalEdge(winRate) {
  const rate = toNumber(winRate)
  if (rate === null) return { label: 'UNKNOWN', ok: false }
  if (rate < 52) return { label: 'WEAK', ok: false }
  if (rate <= 56) return { label: 'SLIGHT', ok: true }
  if (rate <= 60) return { label: 'MODERATE', ok: true }
  return { label: 'STRONG', ok: true }
}

export function labelTone(ok, pending = false) {
  if (pending) return 'border-slate-600 bg-slate-800 text-slate-300'
  return ok
    ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-300'
    : 'border-amber-700/60 bg-amber-900/30 text-amber-300'
}

export function decisionTone(finalDecision) {
  if (finalDecision === 'HIGH_CONVICTION') return 'text-emerald-200'
  if (finalDecision === 'TRADE_CANDIDATE') return 'text-emerald-300'
  if (finalDecision === 'WAIT_FOR_CONFIRMATION') return 'text-amber-300'
  if (finalDecision === 'WATCH') return 'text-sky-300'
  return 'text-slate-300'
}

function firstText(value) {
  if (Array.isArray(value)) return value.filter(Boolean).join(', ')
  return value || ''
}

const GATE_FACTOR_LABELS = {
  quote_stale: 'quote is stale',
  quote_type_penalized: 'quote type is not live or actionable',
  spread_not_acceptable: 'option spread is above the maximum',
  volume_below_minimum: 'option volume is below the minimum',
  liquidity_grade_below_b: 'option liquidity grade is below B',
  historical_win_rate_below_52: 'historical win rate is below 52%',
  historical_confidence_not_satisfied: 'historical confidence is not satisfied',
  chart_signal_not_confirmed: 'chart signal is not confirmed',
  chart_grade_below_trade_candidate: 'chart signal is below TRADE_CANDIDATE',
  chart_side_not_aligned: 'chart side is not aligned',
  contract_type_does_not_match_side: 'contract type does not match the chart direction',
  options_sentiment_not_confirming_or_neutral: 'options sentiment is not confirming or neutral',
  ai_gate_unavailable: 'AI Gate is unavailable',
  ai_gate_request_failed: 'AI Gate request failed',
  ai_gate_disabled: 'AI Gate is disabled',
  openai_api_key_missing: 'OpenAI API key is missing on the backend',
  setup_data_unavailable: 'setup data is unavailable',
  historical_gate_not_supported: 'historical data does not support this setup',
  historical_data_unavailable: 'historical data is unavailable',
  underlying_price_mismatch: 'live underlying price differs from chart data',
  underlying_price_unavailable: 'live underlying price is unavailable',
}

function humanizeGateFactor(factor) {
  const key = String(factor || '').trim().toLowerCase()
  if (!key) return ''
  return GATE_FACTOR_LABELS[key] || key.replace(/_/g, ' ')
}

export function conciseGateFailure(aiGate) {
  if (!aiGate) return 'AI Gate has not returned PROCEED yet.'
  const factors = Array.isArray(aiGate.blocking_factors) ? aiGate.blocking_factors : []
  const readableFactors = factors.map(humanizeGateFactor).filter(Boolean)
  if (readableFactors.length) {
    return `AI Gate did not return PROCEED: ${readableFactors.slice(0, 4).join(', ')}.`
  }
  if (aiGate.trade_explanation?.why_passed_or_failed) {
    return aiGate.trade_explanation.why_passed_or_failed
  }
  if (aiGate.decision && aiGate.decision !== 'PROCEED') {
    return `AI Gate result is ${aiGate.decision}.`
  }
  return aiGate.summary || 'AI Gate has not returned PROCEED yet.'
}

export function gateFailureSummary(aiGate) {
  if (!aiGate) return 'AI Gate has not returned PROCEED yet.'
  if (aiGate.decision !== 'PROCEED') return conciseGateFailure(aiGate)
  return aiGate.summary || 'AI Gate returned PROCEED.'
}

export function closedAiGate(summary, blockingFactors = ['ai_gate_unavailable']) {
  return {
    decision: 'DO_NOT_PROCEED',
    summary,
    blocking_factors: blockingFactors,
  }
}

export function optionWarnings({ side, contract, contracts, backtest, marketSession = null }) {
  const warnings = []
  const expected = expectedContractType(side)
  const contractType = (contract?.type || '').toUpperCase()
  const spread = toNumber(contract?.spread_percentage)
  const volume = toNumber(contract?.volume)
  const winRate = toNumber(backtest?.win_rate_pct)
  const quoteType = (contract?.quote_type || contracts?.quote_type || '').toUpperCase()
  const liveSession = isLiveSession(marketSession)

  if (expected && contractType && contractType !== expected) {
    warnings.push(`${side} bias requires a ${expected}; candidate is ${contractType}.`)
  }
  if (liveSession) {
    if (spread !== null && spread > 5) {
      warnings.push(`Spread is above 5% (${spread.toFixed(2)}%).`)
    }
    if (volume !== null && volume < 100) {
      warnings.push(`Volume is below 100 (${volume}).`)
    }
    if (quoteType && BAD_QUOTE_TYPES.has(quoteType)) {
      warnings.push(`Quote type is ${quoteType}.`)
    }
    if (contract?.quote_stale) {
      warnings.push('Quote is stale.')
    }
  }
  if (winRate !== null && winRate < 52) {
    warnings.push(`Historical win rate is below 52% (${winRate.toFixed(2)}%).`)
  }

  return warnings
}

export function evaluateHistoricalSupport(backtest) {
  const derivedSampleConfidence = classifySampleConfidence(backtest?.occurrences)
  const sampleConfidenceLabel = backtest?.sample_confidence || derivedSampleConfidence.label
  const sampleConfidence = {
    label: sampleConfidenceLabel,
    ok: ['MEDIUM', 'ENOUGH'].includes(sampleConfidenceLabel),
  }
  const derivedHistoricalEdge = classifyHistoricalEdge(backtest?.win_rate_pct)
  const historicalEdgeLabel = backtest?.historical_edge || derivedHistoricalEdge.label
  const historicalEdge = {
    label: historicalEdgeLabel,
    ok: ['SLIGHT', 'MODERATE', 'STRONG'].includes(historicalEdgeLabel),
  }

  let reason = `Historical support is ${historicalEdge.label} with sample confidence ${sampleConfidence.label}.`
  if (!sampleConfidence.ok) {
    reason = `Historical sample confidence is ${sampleConfidence.label}; at least MEDIUM is required before suggesting a trade.`
  } else if (!historicalEdge.ok) {
    reason = `Historical edge is ${historicalEdge.label} (${formatPct(backtest?.win_rate_pct)} win rate); win rate must be at least 52%.`
  }

  return {
    ok: sampleConfidence.ok && historicalEdge.ok,
    sampleConfidence,
    historicalEdge,
    reason,
  }
}

function roundNumber(value, digits = 2) {
  const n = toNumber(value)
  if (n === null) return null
  return Number(n.toFixed(digits))
}

function formatMoney(value) {
  const n = toNumber(value)
  return n === null ? '-' : `$${n.toFixed(2)}`
}

function optionPremium(value) {
  const n = toNumber(value)
  return n === null ? null : Number(n.toFixed(2))
}

function candleDay(row) {
  const time = toNumber(row?.time)
  if (time === null) return ''
  return new Date(time * 1000).toISOString().slice(0, 10)
}

function normalizeCandles(indicatorData) {
  const candleRows = Array.isArray(indicatorData?.candles) ? indicatorData.candles : []
  const overlays = Array.isArray(indicatorData?.indicators) ? indicatorData.indicators : []
  const overlayByTime = new Map()

  overlays.forEach((row) => {
    const time = toNumber(row?.time)
    if (time !== null) overlayByTime.set(time, row)
  })

  const byTime = new Map()
  candleRows.forEach((row) => {
    const time = toNumber(row?.time)
    const open = toNumber(row?.open)
    const high = toNumber(row?.high)
    const low = toNumber(row?.low)
    const close = toNumber(row?.close)
    if (time === null || open === null || high === null || low === null || close === null) return
    byTime.set(time, {
      ...overlayByTime.get(time),
      time,
      open,
      high,
      low,
      close,
      volume: toNumber(row?.volume, 0),
    })
  })

  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

function latestMarketRow(indicatorData, scan) {
  const candles = normalizeCandles(indicatorData)
  const lastCandle = candles.length ? candles[candles.length - 1] : {}
  return {
    ...(scan?.indicators || {}),
    ...lastCandle,
    ...(indicatorData?.latest || {}),
  }
}

function resolveUnderlyingPrice(candidate, contracts) {
  const live = toNumber(candidate?.underlying_price)
  if (live !== null && live > 0) return live
  const contractLevel = toNumber(contracts?.underlying_price)
  if (contractLevel !== null && contractLevel > 0) return contractLevel
  return null
}

function nearestAbove(current, values) {
  const valid = values.map((value) => toNumber(value)).filter((value) => value !== null && value > current)
  return valid.length ? Math.min(...valid) : null
}

function nearestBelow(current, values) {
  const valid = values.map((value) => toNumber(value)).filter((value) => value !== null && value < current)
  return valid.length ? Math.max(...valid) : null
}

function levelContext(side, indicatorData, scan, liveUnderlyingPrice = null) {
  const candles = normalizeCandles(indicatorData)
  const latest = latestMarketRow(indicatorData, scan)
  const live = toNumber(liveUnderlyingPrice)
  const current = (live !== null && live > 0) ? live : null
  const currentSafe = current && current > 0 ? current : 0
  const atrRaw = toNumber(latest?.atr)
  const atr = atrRaw && atrRaw > 0 ? atrRaw : Math.max(currentSafe * 0.01, 0.25)
  const buffer = Math.max(atr * 0.08, currentSafe * 0.0015, 0.01)

  const recent = candles.slice(-20)
  const recentSwingHigh = recent.length ? Math.max(...recent.map((row) => toNumber(row.high, 0))) : null
  const recentSwingLow = recent.length ? Math.min(...recent.map((row) => toNumber(row.low, Infinity))) : null

  const lastCandle = candles.length ? candles[candles.length - 1] : null
  const currentDay = candleDay(lastCandle)
  const dayCandles = currentDay ? candles.filter((row) => candleDay(row) === currentDay) : []
  const opening = dayCandles.length ? dayCandles[0] : null
  const openingRange = dayCandles.slice(0, 3)
  const openingRangeHigh = openingRange.length ? Math.max(...openingRange.map((row) => toNumber(row.high, 0))) : null
  const openingRangeLow = openingRange.length ? Math.min(...openingRange.map((row) => toNumber(row.low, Infinity))) : null
  const openingRangeSize = openingRangeHigh !== null && openingRangeLow !== null && Number.isFinite(openingRangeLow)
    ? Math.max(openingRangeHigh - openingRangeLow, atr * 0.5)
    : atr

  const priorRows = currentDay ? candles.filter((row) => candleDay(row) && candleDay(row) < currentDay) : []
  const priorDay = priorRows.length ? candleDay(priorRows[priorRows.length - 1]) : ''
  const priorDayRows = priorDay ? priorRows.filter((row) => candleDay(row) === priorDay) : []
  const priorDayHigh = priorDayRows.length ? Math.max(...priorDayRows.map((row) => toNumber(row.high, 0))) : null
  const priorDayLow = priorDayRows.length ? Math.min(...priorDayRows.map((row) => toNumber(row.low, Infinity))) : null

  return {
    side: (side || '').toUpperCase(),
    current: currentSafe,
    atr,
    buffer,
    latest,
    opening,
    openingRangeHigh,
    openingRangeLow: Number.isFinite(openingRangeLow) ? openingRangeLow : null,
    openingRangeSize,
    priorDayHigh,
    priorDayLow: Number.isFinite(priorDayLow) ? priorDayLow : null,
    recentSwingHigh,
    recentSwingLow: Number.isFinite(recentSwingLow) ? recentSwingLow : null,
    vwap: toNumber(latest?.vwap),
    emaFast: toNumber(latest?.ema_fast),
    emaSlow: toNumber(latest?.ema_slow),
    emaTrend: toNumber(latest?.ema_trend),
    bbUpper: toNumber(latest?.bb_upper),
    bbMid: toNumber(latest?.bb_mid),
    bbLower: toNumber(latest?.bb_lower),
    volume: toNumber(latest?.volume),
    volumeAvg: toNumber(latest?.volume_avg),
  }
}

export function calculateEntryTrigger(side, indicatorData, scan, liveUnderlyingPrice = null) {
  const ctx = levelContext(side, indicatorData, scan, liveUnderlyingPrice)
  if (!ctx.current) {
    return {
      type: 'breakout',
      price: null,
      condition: 'No reliable underlying trigger is available until current price data refreshes.',
      confirmation_needed: 'Fresh candle data with volume is required before entry.',
    }
  }

  if (ctx.side === 'SHORT') {
    const vwapReject = ctx.vwap !== null && ctx.current >= ctx.vwap
    const support = nearestBelow(ctx.current, [
      ctx.openingRangeLow,
      ctx.recentSwingLow,
      ctx.priorDayLow,
      ctx.bbLower,
      ctx.vwap,
      ctx.emaFast,
      ctx.emaSlow,
    ])
    const price = roundNumber((vwapReject ? ctx.vwap : support) ?? (ctx.current - Math.max(ctx.atr * 0.25, ctx.buffer)), 2)
    return {
      type: vwapReject ? 'vwap_reject' : 'breakdown',
      price,
      condition: `Enter only if price breaks below ${formatMoney(price)} with volume and stays below VWAP.`,
      confirmation_needed: `5-minute candle close below ${formatMoney(price)}, volume above recent average, EMA 8 not reclaiming EMA 21, and MACD still falling.`,
    }
  }

  const vwapReclaim = ctx.vwap !== null && ctx.current <= ctx.vwap
  const resistance = nearestAbove(ctx.current, [
    ctx.openingRangeHigh,
    ctx.recentSwingHigh,
    ctx.priorDayHigh,
    ctx.bbUpper,
    ctx.vwap,
    ctx.emaFast,
    ctx.emaSlow,
  ])
  const price = roundNumber((vwapReclaim ? ctx.vwap : resistance) ?? (ctx.current + Math.max(ctx.atr * 0.25, ctx.buffer)), 2)
  return {
    type: vwapReclaim ? 'vwap_reclaim' : 'breakout',
    price,
    condition: `Enter only if price breaks above ${formatMoney(price)} with volume and holds above VWAP.`,
    confirmation_needed: `5-minute candle close above ${formatMoney(price)}, volume above recent average, EMA 8 holding above EMA 21, and MACD still rising.`,
  }
}

export function calculateInvalidation(side, indicatorData, scan, liveUnderlyingPrice = null) {
  const ctx = levelContext(side, indicatorData, scan, liveUnderlyingPrice)
  if (!ctx.current) {
    return {
      price: null,
      condition: 'Setup is invalid until fresh underlying price data is available.',
    }
  }

  if (ctx.side === 'SHORT') {
    const resistance = nearestAbove(ctx.current, [
      ctx.vwap,
      ctx.openingRangeHigh,
      ctx.recentSwingHigh,
      ctx.emaFast,
      ctx.emaSlow,
      ctx.bbMid,
    ])
    const price = roundNumber(resistance ?? (ctx.current + Math.max(ctx.atr * 0.6, ctx.current * 0.0035)), 2)
    return {
      price,
      condition: `Setup fails if price reclaims ${formatMoney(price)} or closes back above VWAP.`,
    }
  }

  const support = nearestBelow(ctx.current, [
    ctx.vwap,
    ctx.openingRangeLow,
    ctx.recentSwingLow,
    ctx.emaFast,
    ctx.emaSlow,
    ctx.bbMid,
  ])
  const price = roundNumber(support ?? (ctx.current - Math.max(ctx.atr * 0.6, ctx.current * 0.0035)), 2)
  return {
    price,
    condition: `Setup fails if price loses ${formatMoney(price)} or closes back below VWAP.`,
  }
}

export function calculateTargets(side, indicatorData, scan, liveUnderlyingPrice = null) {
  const ctx = levelContext(side, indicatorData, scan, liveUnderlyingPrice)
  const entry = calculateEntryTrigger(side, indicatorData, scan, liveUnderlyingPrice)?.price ?? ctx.current
  if (!ctx.current || entry === null) {
    return {
      target_1: null,
      target_2: null,
      stretch_target: null,
      basis: 'current price data unavailable',
    }
  }

  if (ctx.side === 'SHORT') {
    const first = nearestBelow(entry, [ctx.priorDayLow, ctx.openingRangeLow, ctx.recentSwingLow, ctx.bbLower])
    const target1 = roundNumber(first ?? (entry - Math.max(ctx.atr * 0.5, ctx.current * 0.004)), 2)
    const target2 = roundNumber(Math.min(
      target1 - Math.max(ctx.atr * 0.5, ctx.current * 0.004),
      entry - Math.max(ctx.atr, ctx.current * 0.008),
    ), 2)
    const stretch = roundNumber(target2 - Math.max(ctx.atr * 0.75, ctx.current * 0.006), 2)
    return {
      target_1: target1,
      target_2: target2,
      stretch_target: stretch,
      basis: 'prior low, opening range low, recent support, Bollinger lower band, and ATR extension',
    }
  }

  const first = nearestAbove(entry, [ctx.priorDayHigh, ctx.openingRangeHigh, ctx.recentSwingHigh, ctx.bbUpper])
  const target1 = roundNumber(first ?? (entry + Math.max(ctx.atr * 0.5, ctx.current * 0.004)), 2)
  const target2 = roundNumber(Math.max(
    target1 + Math.max(ctx.atr * 0.5, ctx.current * 0.004),
    entry + Math.max(ctx.atr, ctx.current * 0.008),
  ), 2)
  const stretch = roundNumber(target2 + Math.max(ctx.atr * 0.75, ctx.current * 0.006), 2)
  return {
    target_1: target1,
    target_2: target2,
    stretch_target: stretch,
    basis: 'prior high, opening range high, recent resistance, Bollinger upper band, and ATR extension',
  }
}

export function calculateOptionExecution(candidate, side, marketSession = null) {
  const bid = toNumber(candidate?.bid)
  const ask = toNumber(candidate?.ask)
  const last = toNumber(candidate?.last)
  const spread = toNumber(candidate?.spread_percentage)
  const stale = Boolean(candidate?.quote_stale)
  const liveSession = isLiveSession(marketSession)
  const hasBidAsk = bid !== null && ask !== null && bid > 0 && ask > 0
  const fair = hasBidAsk ? (bid + ask) / 2 : last
  const contractType = (candidate?.type || expectedContractType(side) || '').toUpperCase()
  const candidateContract = candidate?.contract_symbol || [
    contractType,
    candidate?.expiration,
    candidate?.strike ? `${candidate.strike}` : null,
  ].filter(Boolean).join(' ') || '-'

  const expirationText = String(candidate?.expiration || '')
  const today = new Date().toISOString().slice(0, 10)
  const sameDayExpiration = expirationText.slice(0, 10) === today

  const avoid = [
    liveSession
      ? 'Avoid if spread widens above 5% or ask is more than 10% above last fair value.'
      : 'Previous-session pricing only. Refresh the option chain after the next open before treating any premium as actionable.',
    liveSession && spread !== null && spread > 5 ? `Current spread is wide at ${spread.toFixed(2)}%, so do not chase the ask.` : null,
    liveSession && stale ? 'Quote is stale, so premium targets are not reliable until the option quote refreshes.' : null,
    sameDayExpiration ? 'Same-day expiration requires tighter entries, faster exits, and no chasing.' : null,
  ].filter(Boolean).join(' ')

  if (!fair || stale) {
    return {
      candidate_contract: candidateContract,
      max_reasonable_entry: null,
      ideal_entry_zone: liveSession
        ? (stale ? 'Wait for a fresh quote before setting an entry zone.' : 'No reliable bid/ask/last premium is available.')
        : 'Previous session data only; refresh after the next open before setting a premium zone.',
      take_profit_1: null,
      take_profit_2: null,
      stop_premium: null,
      avoid_if: avoid,
    }
  }

  const highZone = hasBidAsk && spread !== null && spread <= 5 ? ask : fair * 1.03
  const lowZone = hasBidAsk ? Math.max(bid, fair * 0.97) : fair * 0.97
  const maxReasonable = hasBidAsk ? Math.min(ask * 1.02, fair * 1.08) : fair * 1.05

  return {
    candidate_contract: candidateContract,
    max_reasonable_entry: optionPremium(maxReasonable),
    ideal_entry_zone: liveSession ? `${formatMoney(lowZone)} - ${formatMoney(highZone)}` : `Previous session only (${formatMoney(lowZone)} - ${formatMoney(highZone)} after refresh)`,
    take_profit_1: optionPremium(fair * 1.25),
    take_profit_2: optionPremium(fair * 1.4),
    stop_premium: optionPremium(fair * 0.75),
    avoid_if: avoid,
  }
}

function blockerFacts({ blockingReasons, candidate, contracts, backtest, aiGate, scan, marketSession }) {
  const facts = []
  const spread = toNumber(candidate?.spread_percentage)
  const maxSpread = toNumber(candidate?.recommended_max_spread_pct ?? contracts?.recommended_max_spread_pct, 5)
  const volume = toNumber(candidate?.volume)
  const minVolume = toNumber(candidate?.minimum_volume ?? contracts?.filters?.min_volume, 100)
  const winRate = toNumber(backtest?.win_rate_pct)
  const quoteType = (candidate?.quote_type || contracts?.quote_type || '').toUpperCase()
  const scanPrice = toNumber(scan?.price)
  const underlyingPrice = toNumber(candidate?.underlying_price ?? contracts?.underlying_price)
  const liveSession = isLiveSession(marketSession)

  if (liveSession && spread !== null && maxSpread !== null && spread > maxSpread) facts.push(`spread is ${spread.toFixed(2)}% against a ${maxSpread.toFixed(2)}% maximum`)
  if (liveSession && volume !== null && volume < minVolume) facts.push(`volume is ${volume}, below the ${minVolume} minimum`)
  if (winRate !== null && winRate < 52) facts.push(`historical win rate is ${winRate.toFixed(2)}%, below 52%`)
  if (liveSession && candidate?.quote_stale) facts.push('the option quote is stale')
  if (liveSession && quoteType && BAD_QUOTE_TYPES.has(quoteType)) facts.push(`quote type is ${quoteType}`)
  if (liveSession && scanPrice !== null && underlyingPrice !== null && scanPrice > 0 && underlyingPrice > 0) {
    const mismatchPct = Math.abs(scanPrice - underlyingPrice) / underlyingPrice * 100
    if (mismatchPct > 1) {
      facts.push(`live underlying quote ${formatMoney(underlyingPrice)} differs from chart data by ${mismatchPct.toFixed(2)}%`)
    }
  }
  if (liveSession && (underlyingPrice === null || underlyingPrice <= 0)) {
    facts.push('live underlying quote is unavailable')
  }
  if (liveSession && aiGate?.decision !== 'PROCEED') facts.push(`AI Gate did not return PROCEED`)

  if (!facts.length) {
    return blockingReasons.map((reason) => reason.replace(/^[^:]+:\s*/, '')).slice(0, 4)
  }
  return facts
}

function explainWatchConditions({ side, trigger, invalidation, candidate, contracts, aiGate, marketSession }) {
  const expected = expectedContractType(side)
  const maxSpread = toNumber(candidate?.recommended_max_spread_pct ?? contracts?.recommended_max_spread_pct, 5)
  const minVolume = toNumber(candidate?.minimum_volume ?? contracts?.filters?.min_volume, 100)
  const directionText = side === 'SHORT' ? 'breaking below' : 'breaking above'
  const vwapText = side === 'SHORT' ? 'rejecting VWAP' : 'holding VWAP'
  const liveSession = isLiveSession(marketSession)
  if (trigger?.price === null || trigger?.price === undefined) {
    return [
      liveSession
        ? 'Watch for the live E*TRADE underlying quote to load before defining a trigger.'
        : 'Planning only. Refresh quotes after the options market opens before entering.',
      liveSession
        ? `Do not act until the ${expected || 'option'} confirms liquidity and the AI Gate can evaluate a live price.`
        : 'Use the next open to refresh the chain and confirm the trigger before entering.',
    ]
  }
  const rows = [
    `Watch for price ${directionText} ${formatMoney(trigger?.price)} on a 5-minute candle with volume above recent average.`,
    `Watch for ${vwapText}; do not act if price immediately rejects the trigger or loses VWAP.`,
    liveSession
      ? `Watch that the selected ${expected || 'option'} stays at or below a ${maxSpread.toFixed(2)}% spread and volume stays at or above ${minVolume}.`
      : 'The option quote shown is previous-session data and must be refreshed after the next open before it is actionable.',
    liveSession && aiGate?.decision !== 'PROCEED' ? 'Watch for the AI Gate to return PROCEED after the setup facts refresh.' : null,
    invalidation?.price !== null ? `Cancel the idea if underlying price trades through ${formatMoney(invalidation.price)}.` : null,
  ].filter(Boolean)
  if (!liveSession && marketSession?.session_note) {
    rows.unshift(marketSession.session_note)
  }
  return rows
}

function upgradeConditions({ finalDecision, side, trigger, candidate, contracts, backtest, aiGate, historicalEdge, sampleConfidence, labels }) {
  const expected = expectedContractType(side)
  const maxSpread = toNumber(candidate?.recommended_max_spread_pct ?? contracts?.recommended_max_spread_pct, 5)
  const minVolume = toNumber(candidate?.minimum_volume ?? contracts?.filters?.min_volume, 100)
  const triggerPrice = toNumber(trigger?.price)
  const triggerAvailable = triggerPrice !== null && triggerPrice > 0
  const conditions = []

  if (labels?.chartSignal === 'WAIT' || finalDecision === 'WAIT_FOR_CONFIRMATION') {
    conditions.push(
      triggerAvailable
        ? `Upgrade only after a 5-minute close ${side === 'SHORT' ? 'below' : 'above'} ${formatMoney(triggerPrice)} with volume above recent average.`
        : 'Upgrade only after the live E*TRADE quote loads and a trigger can be confirmed with volume.'
    )
  }
  if (aiGate?.decision !== 'PROCEED') conditions.push('AI Gate must return PROCEED.')
  if (!historicalEdge?.ok) conditions.push(`Historical edge must improve to at least SLIGHT, with win rate at or above 52%. Current win rate is ${formatPct(backtest?.win_rate_pct)}.`)
  if (!sampleConfidence?.ok) conditions.push('Sample confidence must improve to MEDIUM or ENOUGH.')
  if (candidate) {
    conditions.push(`The selected ${expected || candidate?.type || 'option'} must keep spread at or below ${maxSpread.toFixed(2)}% and volume at or above ${minVolume}.`)
  }
  if (finalDecision === 'TRADE_CANDIDATE') {
    conditions.push('Upgrade toward HIGH_CONVICTION only if the chart remains confirmed and historical edge improves to STRONG.')
  }
  return [...new Set(conditions)].slice(0, 5)
}

function downgradeConditions({ side, trigger, invalidation, candidate, contracts }) {
  const maxSpread = toNumber(candidate?.recommended_max_spread_pct ?? contracts?.recommended_max_spread_pct, 5)
  const minVolume = toNumber(candidate?.minimum_volume ?? contracts?.filters?.min_volume, 100)
  const triggerPrice = toNumber(trigger?.price)
  const triggerAvailable = triggerPrice !== null && triggerPrice > 0
  const invalidationPrice = toNumber(invalidation?.price)
  const invalidationAvailable = invalidationPrice !== null && invalidationPrice > 0
  return [
    triggerAvailable
      ? `Downgrade if price fails to hold the trigger near ${formatMoney(triggerPrice)} after the 5-minute close.`
      : 'Downgrade until the live E*TRADE quote loads and a trigger can be confirmed.',
    invalidationAvailable
      ? `Downgrade immediately if price trades through ${formatMoney(invalidationPrice)}.`
      : 'Downgrade until invalidation can be measured from a live E*TRADE quote.',
    `Downgrade if spread widens above ${maxSpread.toFixed(2)}% or volume drops below ${minVolume}.`,
    'Downgrade if the quote becomes stale, CLOSING, DELAYED, or SANDBOX.',
    'Downgrade if AI Gate returns DO_NOT_PROCEED after refreshed facts.',
  ].filter(Boolean)
}

function cancelConditions({ side, invalidation, candidate, contracts, backtest }) {
  const expected = expectedContractType(side)
  const contractType = (candidate?.type || '').toUpperCase()
  const invalidationPrice = toNumber(invalidation?.price)
  const invalidationAvailable = invalidationPrice !== null && invalidationPrice > 0
  const conditions = [
    invalidationAvailable
      ? `Cancel if underlying price violates ${formatMoney(invalidationPrice)}.`
      : 'Cancel until a live E*TRADE quote is available.',
    'Cancel if the quote is stale or quote type is CLOSING, DELAYED, or SANDBOX.',
    'Cancel if AI Gate returns DO_NOT_PROCEED.',
  ]
  if (expected && contractType && expected !== contractType) conditions.push(`Cancel because ${side} bias requires a ${expected}, not ${contractType}.`)
  if (toNumber(backtest?.win_rate_pct) !== null && toNumber(backtest?.win_rate_pct) < 52) conditions.push('Cancel until historical win rate is at least 52%.')
  if (candidate?.recommendation_eligible === false) conditions.push(`Cancel while contract eligibility blockers remain: ${firstText(candidate.recommendation_blockers)}.`)
  return [...new Set(conditions)].slice(0, 5)
}

export function buildTradeExplanation({
  side,
  scan,
  indicatorData,
  contract,
  contracts,
  backtest,
  aiGate,
  marketSession = null,
  finalDecision,
  approved,
  blockingReasons,
  sampleConfidence,
  historicalEdge,
  labels,
}) {
  const symbol = scan?.symbol || contract?.symbol || contracts?.symbol || 'This setup'
  const normalizedSide = (side || scan?.side || '').toUpperCase()
  const expected = expectedContractType(normalizedSide)
  const candidate = contract || getCandidateContract(normalizedSide, contracts)
  const liveUnderlyingPrice = resolveUnderlyingPrice(candidate, contracts)
  const trigger = calculateEntryTrigger(normalizedSide, indicatorData, scan, liveUnderlyingPrice)
  const invalidation = calculateInvalidation(normalizedSide, indicatorData, scan, liveUnderlyingPrice)
  const targets = calculateTargets(normalizedSide, indicatorData, scan, liveUnderlyingPrice)
  const execution = calculateOptionExecution(candidate, normalizedSide, marketSession)
  const facts = blockerFacts({ blockingReasons, candidate, contracts, backtest, aiGate, scan, marketSession })
  const reasonText = facts.length ? facts.join('; ') : 'the required gates have not all passed yet'
  const triggerDirection = normalizedSide === 'SHORT' ? 'below' : 'above'
  const optionType = expected || candidate?.type || 'option'
  const historicalText = `${historicalEdge?.label || 'UNKNOWN'} historical edge (${formatPct(backtest?.win_rate_pct)} win rate over ${backtest?.occurrences ?? 0} sessions, sample confidence ${sampleConfidence?.label || 'UNKNOWN'})`
  const triggerPrice = toNumber(trigger?.price)
  const triggerAvailable = triggerPrice !== null && triggerPrice > 0
  const liveSession = isLiveSession(marketSession)
  const sessionNote = marketSession?.session_note || ''

  let plainEnglishSummary
  let whyPassedOrFailed

  if (approved) {
    plainEnglishSummary = triggerAvailable
      ? `${symbol} is a ${finalDecision === 'HIGH_CONVICTION' ? 'high-conviction setup' : 'trade candidate'} because the chart signal is confirmed, ${historicalText}, the selected ${optionType} passed liquidity and data checks, and AI Gate returned PROCEED. Entry is valid only ${triggerDirection} ${formatMoney(trigger.price)} after confirmation.`
      : `${symbol} is a ${finalDecision === 'HIGH_CONVICTION' ? 'high-conviction setup' : 'trade candidate'} because the chart signal is confirmed, ${historicalText}, the selected ${optionType} passed liquidity and data checks, and AI Gate returned PROCEED. The live E*TRADE quote is not available yet, so the trigger and invalidation levels are not actionable.`
    whyPassedOrFailed = `This passed because Chart Signal is ${labels?.chartSignal}, Historical Edge is ${historicalEdge?.label}, Option Liquidity passed, Data Quality passed, and AI Gate returned PROCEED.`
  } else if (finalDecision === 'WAIT_FOR_CONFIRMATION') {
    plainEnglishSummary = triggerAvailable
      ? `${liveSession ? '' : 'Planning only. '}${symbol} is waiting for confirmation because the direction is possible but price has not cleared the required level yet. Do not enter until a 5-minute candle closes ${triggerDirection} ${formatMoney(trigger.price)} while VWAP and volume confirm.${!liveSession && sessionNote ? ` ${sessionNote}` : ''}`
      : `${liveSession ? '' : 'Planning only. '}${symbol} is waiting for a refreshed quote before the trigger level can be confirmed. Do not enter until the new session opens and VWAP/volume confirm.${!liveSession && sessionNote ? ` ${sessionNote}` : ''}`
    whyPassedOrFailed = `This was downgraded to WAIT_FOR_CONFIRMATION because ${reasonText}.`
  } else if (finalDecision === 'WATCH') {
    plainEnglishSummary = triggerAvailable
      ? `${liveSession ? '' : 'Planning only. '}${symbol} is only a watch because ${reasonText}. A better ${optionType} or cleaner price confirmation is needed before this can become a trade candidate.${!liveSession && sessionNote ? ` ${sessionNote}` : ''}`
      : `${liveSession ? '' : 'Planning only. '}${symbol} is only a watch because ${reasonText}. A refreshed quote is still needed before this can become a trade candidate.${!liveSession && sessionNote ? ` ${sessionNote}` : ''}`
    whyPassedOrFailed = `This did not receive final approval because ${reasonText}.`
  } else {
    plainEnglishSummary = triggerAvailable
      ? `${liveSession ? '' : 'Planning only. ' }No trade. The setup failed because ${reasonText}. It needs the blocking conditions to clear before it becomes actionable.${!liveSession && sessionNote ? ` ${sessionNote}` : ''}`
      : `${liveSession ? '' : 'Planning only. '}No trade. The setup failed because ${reasonText}. A refreshed quote is still needed before it can be evaluated cleanly.${!liveSession && sessionNote ? ` ${sessionNote}` : ''}`
    whyPassedOrFailed = `This failed because ${reasonText}.`
  }

  return {
    final_decision: finalDecision,
    plain_english_summary: plainEnglishSummary,
    why_passed_or_failed: whyPassedOrFailed,
    watch_for: explainWatchConditions({ side: normalizedSide, trigger, invalidation, candidate, contracts, aiGate, marketSession }),
    entry_trigger: trigger,
    invalidation,
    targets,
    option_execution: execution,
    underlying_reference: liveUnderlyingPrice
      ? {
          source: liveSession ? 'etrade_live' : 'previous_session_or_extended_hours',
          label: liveSession ? 'Live E*TRADE price' : (marketSession?.session_state === 'PREMARKET' || marketSession?.session_state === 'AFTER_HOURS' ? 'Extended-hours reference' : 'Previous session reference'),
          price: liveUnderlyingPrice,
        }
      : {
          source: liveSession ? 'etrade_live_unavailable' : 'previous_session_unavailable',
          label: liveSession ? 'Live E*TRADE price unavailable' : 'Previous session price unavailable',
          price: null,
        },
    market_session: marketSession || null,
    upgrade_conditions: upgradeConditions({
      finalDecision,
      side: normalizedSide,
      trigger,
      candidate,
      contracts,
      backtest,
      aiGate,
      historicalEdge,
      sampleConfidence,
      labels,
    }),
    downgrade_conditions: downgradeConditions({ side: normalizedSide, trigger, invalidation, candidate, contracts }),
    cancel_conditions: cancelConditions({ side: normalizedSide, invalidation, candidate, contracts, backtest }),
  }
}

export function evaluateTradeSetup({ side, scan, indicatorData, contract, contracts, backtest, aiGate, marketSession = null }) {
  const normalizedSide = (side || scan?.side || '').toUpperCase()
  const candidate = contract || getCandidateContract(normalizedSide, contracts)
  const liveUnderlyingPrice = resolveUnderlyingPrice(candidate, contracts)
  const expected = expectedContractType(normalizedSide)
  const chartGrade = (scan?.grade || '').toUpperCase()
  const chartSide = (scan?.side || '').toUpperCase()
  const chartOk = Boolean(
    expected &&
    chartSide === normalizedSide &&
    CONFIRMED_CHART_GRADES.has(chartGrade)
  )

  const historicalSupport = evaluateHistoricalSupport(backtest)
  const sampleConfidence = historicalSupport.sampleConfidence
  const historicalEdge = historicalSupport.historicalEdge
  const liveSession = isLiveSession(marketSession)

  const plannedTrigger = calculateEntryTrigger(normalizedSide, indicatorData, scan, liveUnderlyingPrice)
  const plannedInvalidation = calculateInvalidation(normalizedSide, indicatorData, scan, liveUnderlyingPrice)
  const plannedTargets = calculateTargets(normalizedSide, indicatorData, scan, liveUnderlyingPrice)
  const exitPlanComplete = [
    plannedTrigger?.price,
    plannedInvalidation?.price,
    plannedTargets?.[0]?.price,
    plannedTargets?.[1]?.price,
  ].every((value) => toNumber(value) !== null)

  const blockers = []
  const dataBlockers = []
  const liquidityBlockers = []
  const contractType = (candidate?.type || '').toUpperCase()
  const maxSpread = toNumber(
    candidate?.recommended_max_spread_pct ?? contracts?.recommended_max_spread_pct,
    5
  )
  const spread = toNumber(candidate?.spread_percentage)
  const minVolume = toNumber(candidate?.minimum_volume ?? contracts?.filters?.min_volume, 100)
  const volume = toNumber(candidate?.volume)
  const quoteType = (candidate?.quote_type || contracts?.quote_type || '').toUpperCase()
  const liquidityGrade = (candidate?.liquidity_grade || '').toUpperCase()
  const scanPrice = toNumber(scan?.price)
  const underlyingPrice = toNumber(candidate?.underlying_price ?? contracts?.underlying_price)

  if (!expected) {
    blockers.push('Chart Signal: no LONG or SHORT bias is active.')
  } else if (!chartOk) {
    blockers.push('Chart Signal: waiting for TRADE_CANDIDATE or HIGH_CONVICTION alignment.')
  }

  if (!exitPlanComplete) {
    blockers.push('Exit Plan: entry trigger, invalidation, Target 1, and Target 2 must be defined before approval.')
  }

  if (!candidate) {
    liquidityBlockers.push(`Option Liquidity: no ${expected || 'option'} candidate is available.`)
  } else {
    if (expected && contractType !== expected) {
      blockers.push(`Option Liquidity: ${normalizedSide} bias requires a ${expected}; candidate is ${contractType || 'UNKNOWN'}.`)
    }
    if (liveSession) {
      if (spread === null || spread > maxSpread) {
        liquidityBlockers.push(`Option Liquidity: spread ${spread === null ? 'is unavailable' : `${spread.toFixed(2)}%`} exceeds the ${maxSpread.toFixed(2)}% maximum.`)
      }
      if (volume === null || volume < minVolume) {
        liquidityBlockers.push(`Option Liquidity: volume ${volume === null ? 'is unavailable' : volume} is below the ${minVolume} minimum.`)
      }
      if (liquidityGrade && !['A', 'B'].includes(liquidityGrade)) {
        liquidityBlockers.push(`Option Liquidity: liquidity grade is ${liquidityGrade}; A or B is required.`)
      }
      if (candidate.quote_stale) {
        dataBlockers.push('Data Quality: quote is stale.')
      }
      if (quoteType && BAD_QUOTE_TYPES.has(quoteType)) {
        dataBlockers.push(`Data Quality: quote type is ${quoteType}.`)
      }
      if (scanPrice !== null && underlyingPrice !== null && scanPrice > 0 && underlyingPrice > 0) {
        const mismatchPct = Math.abs(scanPrice - underlyingPrice) / underlyingPrice * 100
        if (mismatchPct > 1) {
          dataBlockers.push(`Data Quality: live underlying quote ${formatMoney(underlyingPrice)} differs from chart data by ${mismatchPct.toFixed(2)}%.`)
        }
      }
      if (underlyingPrice === null || underlyingPrice <= 0) {
        dataBlockers.push('Data Quality: live E*TRADE underlying price is unavailable.')
      }
    }
  }

  if (!historicalEdge.ok) {
    blockers.push(`Historical Edge: ${historicalEdge.label} (${formatPct(backtest?.win_rate_pct)} win rate).`)
  }
  if (!sampleConfidence.ok) {
    blockers.push(`Historical Edge: sample confidence is ${sampleConfidence.label}.`)
  }
  blockers.push(...liquidityBlockers, ...dataBlockers)
  if (liveSession && aiGate?.decision !== 'PROCEED') {
    blockers.push(`AI Gate: ${gateFailureSummary(aiGate)}`)
  }

  if (!liveSession && marketSession?.session_note) {
    blockers.unshift(`Session: ${marketSession.session_note}`)
  }

  let finalDecision = 'WATCH'
  if (!expected || !candidate) {
    finalDecision = 'NO_TRADE'
  } else if (!historicalEdge.ok || !sampleConfidence.ok) {
    finalDecision = 'WATCH'
  } else if (!chartOk) {
    finalDecision = liveSession ? 'WAIT_FOR_CONFIRMATION' : 'WATCH'
  } else if (!liveSession) {
    finalDecision = 'WAIT_FOR_CONFIRMATION'
  } else if (!blockers.length) {
    finalDecision = chartGrade === 'HIGH_CONVICTION' && historicalEdge.label === 'STRONG'
      ? 'HIGH_CONVICTION'
      : 'TRADE_CANDIDATE'
  }

  const approved = liveSession && APPROVED_FINAL_DECISIONS.has(finalDecision) && aiGate?.decision === 'PROCEED'
  const labels = {
    chartSignal: chartOk ? chartGrade : 'WAIT',
    historicalEdge: historicalEdge.label,
    optionLiquidity: !liveSession ? 'PREVIOUS_SESSION' : (liquidityBlockers.length || !candidate ? 'BLOCKED' : 'PASS'),
    dataQuality: !liveSession ? 'PREVIOUS_SESSION' : (dataBlockers.length ? 'BLOCKED' : 'PASS'),
    aiGate: aiGate?.decision || 'PENDING',
    finalDecision,
  }
  const tradeExplanation = buildTradeExplanation({
    side: normalizedSide,
    scan,
    indicatorData,
    contract: candidate,
    contracts,
    backtest,
    aiGate,
    marketSession,
    liveUnderlyingPrice,
    finalDecision,
    approved,
    blockingReasons: blockers,
    sampleConfidence,
    historicalEdge,
    labels,
  })

  return {
    side: normalizedSide,
    expectedContractType: expected,
    contract: candidate,
    finalDecision,
    approved,
    blockingReasons: blockers,
    primaryBlockingReason: blockers[0] || '',
    optionWarnings: optionWarnings({ side: normalizedSide, contract: candidate, contracts, backtest, marketSession }),
    sampleConfidence,
    historicalEdge,
    tradeExplanation,
    labels,
  }
}
