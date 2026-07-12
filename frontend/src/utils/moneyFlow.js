export function toNumber(value, fallback = null) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function normalizeCandles(candles) {
  if (!Array.isArray(candles)) return []
  return candles
    .map((row) => ({
      time: toNumber(row?.time),
      open: toNumber(row?.open),
      high: toNumber(row?.high),
      low: toNumber(row?.low),
      close: toNumber(row?.close),
      volume: toNumber(row?.volume, 0),
    }))
    .filter((row) => row.time !== null && row.open !== null && row.high !== null && row.low !== null && row.close !== null)
    .sort((a, b) => a.time - b.time)
}

function latestIndicator(scan, indicatorData) {
  if (indicatorData?.latest) return indicatorData.latest
  if (Array.isArray(indicatorData?.indicators) && indicatorData.indicators.length) {
    return indicatorData.indicators[indicatorData.indicators.length - 1]
  }
  return scan?.indicators || {}
}

function prevIndicator(indicatorData) {
  if (Array.isArray(indicatorData?.indicators) && indicatorData.indicators.length > 1) {
    return indicatorData.indicators[indicatorData.indicators.length - 2]
  }
  return {}
}

function currentSessionLabel(marketSession) {
  const state = String(marketSession?.session_state || '').toUpperCase()
  const actionable = marketSession == null ? true : Boolean(marketSession?.actionable_live_quotes)
  if (!actionable || ['PREMARKET', 'AFTER_HOURS', 'MARKET_CLOSED', 'HOLIDAY'].includes(state)) {
    return { state, label: 'Previous session', actionable: false }
  }
  return { state, label: 'Live', actionable: true }
}

function optionsAlignmentScore(positioning, side) {
  const score = toNumber(positioning?.bias_score, 0) || 0
  if (!score) return 0
  if (String(side || '').toUpperCase() === 'SHORT') return Math.max(-10, Math.min(8, score * -1))
  return Math.max(-10, Math.min(8, score))
}

function classifyFlow(score, availableCount, conflicted) {
  if (!availableCount) return 'INSUFFICIENT DATA'
  if (conflicted && Math.abs(score) < 25) return 'CONFLICTED'
  if (score >= 55) return 'STRONG ACCUMULATION'
  if (score >= 20) return 'MODERATE ACCUMULATION'
  if (score <= -55) return 'STRONG DISTRIBUTION'
  if (score <= -20) return 'MODERATE DISTRIBUTION'
  return conflicted ? 'CONFLICTED' : 'NEUTRAL'
}

export function buildMoneyFlow({
  symbol,
  side = null,
  marketSession = null,
  scan = null,
  indicatorData = null,
  contracts = null,
  ratios = null,
  position = null,
  trade = null,
  moneyFlow = null,
  benchmarkData = null,
} = {}) {
  if (moneyFlow) return moneyFlow
  if (position?.money_flow) return position.money_flow
  if (trade?.money_flow) return trade.money_flow

  const session = currentSessionLabel(marketSession)
  const candles = normalizeCandles(indicatorData?.candles)
  const latest = latestIndicator(scan, indicatorData)
  const previous = prevIndicator(indicatorData)
  const currentPrice = toNumber(
    latest?.close ??
    indicatorData?.latest?.close ??
    scan?.price ??
    position?.underlying_price ??
    position?.underlying_quote?.price,
  )
  const prevClose = candles.length > 1 ? candles[candles.length - 2].close : toNumber(previous?.close)
  const latestClose = toNumber(latest?.close, currentPrice)
  const priceChange = latestClose !== null && prevClose !== null ? Number((latestClose - prevClose).toFixed(4)) : null
  const priceChangePct = latestClose !== null && prevClose ? Number((((latestClose - prevClose) / prevClose) * 100).toFixed(2)) : null
  const volume = toNumber(latest?.volume, 0) || 0
  const volumeAvg = toNumber(latest?.volume_avg, null)
  const relativeVolume = volumeAvg ? Number((volume / volumeAvg).toFixed(2)) : null
  const dollarVolume = latestClose !== null ? Number((latestClose * volume).toFixed(2)) : null
  const vwap = toNumber(latest?.vwap ?? position?.vwap, null)
  const prevVwap = toNumber(previous?.vwap, null)
  const aboveVwap = latestClose !== null && vwap !== null ? latestClose >= vwap : null
  const vwapSlope = vwap !== null && prevVwap !== null ? Number((vwap - prevVwap).toFixed(4)) : null
  const distanceFromVwapPct = latestClose !== null && vwap ? Number((((latestClose - vwap) / vwap) * 100).toFixed(2)) : null

  const optionsPositioning = ratios?.positioning || contracts?.positioning || position?.options_positioning || {}
  const positioningScore = optionsAlignmentScore(optionsPositioning, side)

  const priceVolumeScore = priceChangePct === null
    ? null
    : Math.max(-25, Math.min(25, priceChangePct * 8 + ((relativeVolume || 0) > 1.5 ? (priceChangePct > 0 ? 10 : -10) : 0)))
  const vwapScore = aboveVwap === null
    ? null
    : Math.max(-20, Math.min(20, (aboveVwap ? 10 : -10) + (vwapSlope === null ? 0 : (vwapSlope > 0 ? 10 : -10))))
  const accumulationScore = candles.length > 1
    ? Math.max(-10, Math.min(10, ((latest?.rsi || 50) - 50) / 5))
    : null

  const components = [
    { name: 'price_volume', weight: 25, score: priceVolumeScore, available: priceVolumeScore !== null },
    { name: 'vwap', weight: 20, score: vwapScore, available: vwapScore !== null },
    { name: 'relative_strength', weight: 20, score: null, available: false },
    { name: 'trade_pressure', weight: 15, score: null, available: false },
    { name: 'accumulation', weight: 10, score: accumulationScore, available: accumulationScore !== null },
    { name: 'options_positioning', weight: 10, score: positioningScore, available: positioningPositionAvailable(optionsPositioning) },
  ]

  const available = components.filter((row) => row.available && row.score !== null)
  const totalWeight = available.reduce((sum, row) => sum + row.weight, 0)
  const weightedScore = totalWeight
    ? Number((available.reduce((sum, row) => sum + (row.score * row.weight), 0) / totalWeight).toFixed(2))
    : 0

  const evidenceOfBuyingPressure = []
  const evidenceOfSellingPressure = []
  const conflictingEvidence = []
  if (priceVolumeScore !== null) {
    if (priceVolumeScore > 0) evidenceOfBuyingPressure.push('Price is rising with supportive volume.')
    if (priceVolumeScore < 0) evidenceOfSellingPressure.push('Price is falling with expanding volume.')
  }
  if (aboveVwap === true) evidenceOfBuyingPressure.push('Price is above VWAP.')
  if (aboveVwap === false) evidenceOfSellingPressure.push('Price is below VWAP.')
  if (optionsPositioning?.bias === 'CALL') evidenceOfBuyingPressure.push('Options positioning leans call-heavy.')
  if (optionsPositioning?.bias === 'PUT') evidenceOfSellingPressure.push('Options positioning leans put-heavy.')
  if (side && optionsPositioning?.bias) {
    const sideUpper = String(side || '').toUpperCase()
    if (sideUpper === 'LONG' && optionsPositioning.bias === 'PUT') conflictingEvidence.push('Options positioning conflicts with a long setup.')
    if (sideUpper === 'SHORT' && optionsPositioning.bias === 'CALL') conflictingEvidence.push('Options positioning conflicts with a short setup.')
  }

  const activeCount = available.length
  const conflicted = evidenceOfBuyingPressure.length > 0 && evidenceOfSellingPressure.length > 0 && Math.abs(weightedScore) < 25
  const classification = classifyFlow(weightedScore, activeCount, conflicted)
  const confidence = activeCount >= 4 && Math.abs(weightedScore) >= 25 ? 'HIGH' : activeCount >= 2 ? 'MEDIUM' : 'LOW'
  const alignment = weightedScore >= 20 ? 'aligned' : weightedScore <= -20 ? 'conflicted' : 'neutral'
  const positionAligned = String(side || '').toUpperCase() === 'LONG'
    ? weightedScore >= 10
    : String(side || '').toUpperCase() === 'SHORT'
      ? weightedScore <= -10
      : null

  const marketStatus = session.actionable ? 'FRESH' : 'PREVIOUS_SESSION'
  const flowAdvice = (() => {
    if (classification === 'INSUFFICIENT DATA') return 'Supports waiting for a refresh before acting on the flow read.'
    if (classification === 'CONFLICTED') return 'Supports waiting or reducing risk until price and flow agree.'
    if (weightedScore > 20) return 'Supports holding or waiting for confirmation, but do not chase an extended move.'
    if (weightedScore < -20) return 'Supports reducing exposure or avoiding a fresh add until price confirms.'
    return 'Supports waiting for confirmation.'
  })()

  return {
    symbol,
    side,
    session_state: session.state,
    session_label: session.label,
    market_status: marketStatus,
    data_freshness: marketStatus,
    classification,
    score: weightedScore,
    confidence,
    alignment,
    position_aligned: positionAligned,
    price_confirmation: {
      price_change: priceChange,
      price_change_pct: priceChangePct,
      volume,
      relative_volume: relativeVolume,
      dollar_volume: dollarVolume,
      volume_vs_same_time_of_day: 'unavailable',
      price_movement_per_1k_shares: priceChange !== null && volume > 0 ? Number(((priceChange / volume) * 1000).toFixed(4)) : null,
      rising_volume_on_up_candles: priceChange !== null ? priceChange > 0 : null,
      source: candles.length ? 'observed' : 'unavailable',
      timestamp: latest?.timestamp || scan?.timestamp || position?.quote_timestamp || null,
      data_status: candles.length ? 'observed' : 'unavailable',
    },
    volume_confirmation: {
      up_candle_volume_share_pct: null,
      down_candle_volume_share_pct: null,
      source: candles.length ? 'observed' : 'unavailable',
      data_status: candles.length ? 'observed' : 'unavailable',
    },
    vwap_behavior: {
      source: vwap !== null ? 'observed' : 'unavailable',
      data_status: vwap !== null ? 'observed' : 'unavailable',
      above_vwap: aboveVwap,
      vwap_slope: vwapSlope,
      holds: null,
      rejections: null,
      distance_from_vwap_pct: distanceFromVwapPct,
      reclaim_or_lose_with_volume: null,
      current_vwap: vwap,
      previous_vwap: prevVwap,
      session_state: session.state,
      actionable_live_quotes: session.actionable,
    },
    relative_strength: {
      source: benchmarkData?.source || benchmarkData?.provider || 'unavailable',
      data_status: benchmarkData ? 'observed' : 'unavailable',
      reason: benchmarkData ? null : 'Benchmark data was not provided',
    },
    trade_pressure: {
      source: 'unavailable',
      data_status: 'unavailable',
      reason: 'Trade-level bid/ask classification was not provided',
    },
    order_book: {
      source: 'unavailable',
      data_status: 'unavailable',
      reason: 'Level II data was not provided',
    },
    accumulation: {
      source: candles.length ? 'observed' : 'unavailable',
      data_status: candles.length ? 'observed' : 'unavailable',
      obv: null,
      obv_slope: null,
      accumulation_distribution_line: null,
      accumulation_distribution_slope: null,
      chaikin_money_flow: null,
      money_flow_index: latest?.rsi ?? null,
      anchored_vwap: vwap,
      up_volume: null,
      down_volume: null,
      close_location_within_range_pct: null,
    },
    options_alignment: {
      source: optionsPositioning?.source || 'unavailable',
      data_status: optionsPositioning?.classification ? 'observed' : 'unavailable',
      classification: optionsPositioning?.classification || 'Insufficient data',
      bias: optionsPositioning?.bias || 'INSUFFICIENT_DATA',
      bias_score: optionsPositioning?.bias_score || 0,
      alignment_score: positioningScore,
      confidence: optionsPositioning?.confidence || 'LOW',
      notes: optionsPositioning?.notes || [],
      scopes: optionsPositioning?.scopes || {},
      baseline: optionsPositioning?.baseline || {},
      selected_expiration: optionsPositioning?.selected_expiration || null,
    },
    components,
    evidence_of_buying_pressure: evidenceOfBuyingPressure,
    evidence_of_selling_pressure: evidenceOfSellingPressure,
    conflicting_evidence: conflictingEvidence,
    what_would_confirm_direction: side ? [
      side.toUpperCase() === 'LONG'
        ? 'A long needs price to hold above VWAP and continue making higher highs.'
        : 'A short needs price to stay below VWAP and continue making lower lows.',
      'Use a fresh 5-minute close through the trigger with volume still expanding.',
      'Require VWAP to hold in the intended direction before adding risk.',
    ] : [
      'Use a fresh 5-minute close through the trigger with volume still expanding.',
      'Require VWAP to hold in the intended direction before adding risk.',
    ],
    what_would_invalidate_direction: side ? [
      side.toUpperCase() === 'LONG'
        ? 'A long fails if price loses VWAP or the recent swing low.'
        : 'A short fails if price reclaims VWAP or the recent swing high.',
      'Treat a failed VWAP reclaim or a clean loss of the trigger level as a setup failure.',
      'Do not add if the benchmark turns sharply against the move and the ticker stops confirming.',
    ] : [
      'Treat a failed VWAP reclaim or a clean loss of the trigger level as a setup failure.',
      'Do not add if the benchmark turns sharply against the move and the ticker stops confirming.',
    ],
    position_advice: flowAdvice,
    data_quality: {
      source: candles.length ? 'observed' : 'unavailable',
      timestamp: latest?.timestamp || scan?.timestamp || position?.quote_timestamp || null,
      session: session.state,
      status: candles.length ? 'observed' : 'unavailable',
      confidence,
    },
  }
}


function positioningPositionAvailable(positioning) {
  return Boolean(positioning && (positioning.classification || positioning.bias_score !== undefined))
}
