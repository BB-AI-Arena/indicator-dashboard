const EASTERN_TIME_ZONE = 'America/New_York'

function toNumber(value, fallback = null) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function parseApiTimestamp(value) {
  if (!value) return null
  if (value instanceof Date) return value
  if (typeof value === 'number') {
    return new Date(value > 1_000_000_000_000 ? value : value * 1000)
  }

  const raw = String(value).trim()
  if (!raw) return null

  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)
  const normalized = hasTimezone ? raw : `${raw}Z`
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function easternParts(value) {
  const date = parseApiTimestamp(value)
  if (!date) return null
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: EASTERN_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const out = {}
  parts.forEach((part) => {
    if (part.type !== 'literal') out[part.type] = part.value
  })
  return out
}

function easternDateKey(value) {
  const parts = easternParts(value)
  if (!parts) return null
  return `${parts.year}-${parts.month}-${parts.day}`
}

function easternMinutes(value) {
  const parts = easternParts(value)
  if (!parts) return null
  return (Number(parts.hour) * 60) + Number(parts.minute)
}

function easternTimestampLabel(value) {
  const date = parseApiTimestamp(value)
  if (!date) return '-'
  return new Intl.DateTimeFormat('en-US', {
    timeZone: EASTERN_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(date)
}

function formatMoney(value, digits = 2) {
  const n = toNumber(value)
  return n === null ? '-' : `$${n.toFixed(digits)}`
}

function formatPct(value, digits = 2) {
  const n = toNumber(value)
  return n === null ? '-' : `${n.toFixed(digits)}%`
}

function normalizeCandles(indicatorData) {
  const rows = Array.isArray(indicatorData?.candles) ? indicatorData.candles : []
  const indicators = Array.isArray(indicatorData?.indicators) ? indicatorData.indicators : []
  const byTime = new Map()
  const indicatorByTime = new Map()

  indicators.forEach((row) => {
    const time = toNumber(row?.time)
    if (time !== null) indicatorByTime.set(time, row)
  })

  rows.forEach((row) => {
    const time = toNumber(row?.time)
    const open = toNumber(row?.open)
    const high = toNumber(row?.high)
    const low = toNumber(row?.low)
    const close = toNumber(row?.close)
    if (time === null || open === null || high === null || low === null || close === null) return
    const overlay = indicatorByTime.get(time) || {}
    byTime.set(time, {
      ...overlay,
      time,
      open,
      high,
      low,
      close,
      volume: toNumber(row?.volume, 0) || 0,
    })
  })

  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

function latestIndicator(indicatorData) {
  if (indicatorData?.latest && typeof indicatorData.latest === 'object') return indicatorData.latest
  if (Array.isArray(indicatorData?.indicators) && indicatorData.indicators.length) {
    return indicatorData.indicators[indicatorData.indicators.length - 1] || {}
  }
  return {}
}

function average(values) {
  const rows = values.map((value) => toNumber(value)).filter((value) => value !== null)
  if (!rows.length) return null
  return rows.reduce((sum, value) => sum + value, 0) / rows.length
}

function classifyFreshness(timestamp, marketSession) {
  const parsed = parseApiTimestamp(timestamp)
  if (!parsed) return 'UNKNOWN_TIMESTAMP'
  if (!marketSession?.actionable_live_quotes) return 'PREVIOUS_SESSION'
  const ageSeconds = Math.max(0, (Date.now() - parsed.getTime()) / 1000)
  if (ageSeconds <= 30) return 'LIVE'
  if (ageSeconds <= 120) return 'AGING'
  return 'STALE'
}

function trendState(latest, currentPrice) {
  const emaFast = toNumber(latest?.ema_fast)
  const emaSlow = toNumber(latest?.ema_slow)
  const emaTrend = toNumber(latest?.ema_trend)
  const vwap = toNumber(latest?.vwap)
  const vwapSlope = toNumber(latest?.vwap_slope)
  const price = toNumber(currentPrice, toNumber(latest?.close))

  const bullish = [emaFast, emaSlow, emaTrend].every((value) => value !== null) && emaFast > emaSlow && emaSlow > emaTrend && price !== null && vwap !== null && price >= vwap
  const bearish = [emaFast, emaSlow, emaTrend].every((value) => value !== null) && emaFast < emaSlow && emaSlow < emaTrend && price !== null && vwap !== null && price <= vwap

  if (bullish) {
    return {
      label: 'BULLISH',
      description: 'EMA 9 > EMA 21 > EMA 50 with price above VWAP.',
      direction: 'LONG',
      score: 1,
    }
  }
  if (bearish) {
    return {
      label: 'BEARISH',
      description: 'EMA 9 < EMA 21 < EMA 50 with price below VWAP.',
      direction: 'SHORT',
      score: -1,
    }
  }
  if (vwapSlope !== null && vwapSlope > 0) {
    return { label: 'MIXED', description: 'VWAP is rising but the EMAs are not fully aligned.', direction: 'LONG', score: 0.5 }
  }
  if (vwapSlope !== null && vwapSlope < 0) {
    return { label: 'MIXED', description: 'VWAP is falling but the EMAs are not fully aligned.', direction: 'SHORT', score: -0.5 }
  }
  return { label: 'NEUTRAL', description: 'No clean EMA/VWAP alignment yet.', direction: null, score: 0 }
}

function marketRegime(latest, currentPrice, trend, candles) {
  const price = toNumber(currentPrice, toNumber(latest?.close))
  const vwap = toNumber(latest?.vwap)
  const atr = toNumber(latest?.atr)
  const closeChangePct = candles.length > 1
    ? ((toNumber(candles[candles.length - 1]?.close, price) || 0) - (toNumber(candles[0]?.close, price) || 0)) / (toNumber(candles[0]?.close, price) || 1) * 100
    : 0

  if (atr !== null && price !== null && price > 0 && atr / price >= 0.02) {
    return { label: 'HIGH_VOLATILITY', description: 'ATR is elevated relative to price.' }
  }
  if (trend.label === 'BULLISH' && price !== null && vwap !== null && price > vwap) {
    return { label: 'TRENDING', description: 'Price is holding above a bullish 15-minute structure.' }
  }
  if (trend.label === 'BEARISH' && price !== null && vwap !== null && price < vwap) {
    return { label: 'TRENDING', description: 'Price is holding below a bearish 15-minute structure.' }
  }
  if (Math.abs(closeChangePct) < 1.25 && atr !== null && price !== null && vwap !== null && Math.abs(price - vwap) / price < 0.01) {
    return { label: 'RANGE_BOUND', description: 'Price is oscillating around VWAP with limited displacement.' }
  }
  return { label: trend.direction ? 'TRANSITIONAL' : 'RANGE_BOUND', description: 'The session is not yet cleanly trending.' }
}

function deriveSessionBounds(candles, marketSession) {
  if (!candles.length) {
    return {
      previous_day_high: null,
      previous_day_low: null,
      premarket_high: null,
      premarket_low: null,
      current_session_high: null,
      current_session_low: null,
      opening_range_high: null,
      opening_range_low: null,
      session_candles: [],
      premarket_candles: [],
      previous_day_candles: [],
    }
  }

  const sessionState = String(marketSession?.session_state || '').toUpperCase()
  const currentSessionDate = easternDateKey(marketSession?.current_eastern_timestamp || candles[candles.length - 1]?.time)
  const regularOpen = easternMinutes(marketSession?.regular_session_open)
  const regularClose = easternMinutes(marketSession?.regular_session_close)
  const sessionCandles = []
  const premarketCandles = []
  const previousDayBuckets = new Map()

  candles.forEach((candle) => {
    const dateKey = easternDateKey(candle.time)
    const minute = easternMinutes(candle.time)
    if (!dateKey || minute === null) return
    if (dateKey < currentSessionDate) {
      const list = previousDayBuckets.get(dateKey) || []
      list.push(candle)
      previousDayBuckets.set(dateKey, list)
      return
    }

    if (regularOpen !== null && minute < regularOpen) {
      premarketCandles.push(candle)
      return
    }
    if (regularOpen !== null && regularClose !== null && minute >= regularOpen && minute < regularClose) {
      sessionCandles.push(candle)
      return
    }
  })

  const previousDayKeys = [...previousDayBuckets.keys()].sort()
  const previousDayCandles = previousDayKeys.length ? previousDayBuckets.get(previousDayKeys[previousDayKeys.length - 1]) || [] : []
  const sessionHigh = sessionCandles.length ? Math.max(...sessionCandles.map((row) => Number(row.high) || 0)) : null
  const sessionLow = sessionCandles.length ? Math.min(...sessionCandles.map((row) => Number(row.low) || 0)) : null
  const premarketHigh = premarketCandles.length ? Math.max(...premarketCandles.map((row) => Number(row.high) || 0)) : null
  const premarketLow = premarketCandles.length ? Math.min(...premarketCandles.map((row) => Number(row.low) || 0)) : null
  const previousDayHigh = previousDayCandles.length ? Math.max(...previousDayCandles.map((row) => Number(row.high) || 0)) : null
  const previousDayLow = previousDayCandles.length ? Math.min(...previousDayCandles.map((row) => Number(row.low) || 0)) : null
  const openingRangeCandles = sessionCandles.slice(0, 2)
  const openingRangeHigh = openingRangeCandles.length ? Math.max(...openingRangeCandles.map((row) => Number(row.high) || 0)) : null
  const openingRangeLow = openingRangeCandles.length ? Math.min(...openingRangeCandles.map((row) => Number(row.low) || 0)) : null

  return {
    session_state: sessionState || 'UNKNOWN',
    previous_day_high: previousDayHigh,
    previous_day_low: previousDayLow,
    premarket_high: premarketHigh,
    premarket_low: premarketLow,
    current_session_high: sessionHigh,
    current_session_low: sessionLow,
    opening_range_high: openingRangeHigh,
    opening_range_low: openingRangeLow,
    session_candles: sessionCandles,
    premarket_candles: premarketCandles,
    previous_day_candles: previousDayCandles,
  }
}

function detectZigZagPivots(candles, thresholdAbs) {
  if (!candles.length) return []
  const pivots = []
  let candidateHigh = { index: 0, price: candles[0].high }
  let candidateLow = { index: 0, price: candles[0].low }
  let trend = null

  const pushPivot = (type, candidate) => {
    const row = candles[candidate.index]
    if (!row) return
    const payload = {
      type,
      index: candidate.index,
      time: row.time,
      price: type === 'HIGH' ? row.high : row.low,
      volume: Number(row.volume) || 0,
    }
    const last = pivots[pivots.length - 1]
    if (last && last.type === payload.type) {
      if ((payload.type === 'HIGH' && payload.price >= last.price) || (payload.type === 'LOW' && payload.price <= last.price)) {
        pivots[pivots.length - 1] = payload
      }
      return
    }
    pivots.push(payload)
  }

  for (let i = 1; i < candles.length; i += 1) {
    const row = candles[i]
    if (row.high >= candidateHigh.price) candidateHigh = { index: i, price: row.high }
    if (row.low <= candidateLow.price) candidateLow = { index: i, price: row.low }

    if (trend !== 'down' && candidateHigh.price - row.low >= thresholdAbs) {
      pushPivot('HIGH', candidateHigh)
      trend = 'down'
      candidateLow = { index: i, price: row.low }
      continue
    }

    if (trend !== 'up' && row.high - candidateLow.price >= thresholdAbs) {
      pushPivot('LOW', candidateLow)
      trend = 'up'
      candidateHigh = { index: i, price: row.high }
    }
  }

  return pivots.sort((a, b) => a.index - b.index)
}

function selectAnchorPair(candles, side, latest, manualAnchors) {
  if (manualAnchors?.low?.price && manualAnchors?.high?.price) {
    return {
      direction: String(side || '').toUpperCase() === 'SHORT' ? 'BEARISH' : 'BULLISH',
      low: {
        time: manualAnchors.low.time || null,
        price: toNumber(manualAnchors.low.price),
      },
      high: {
        time: manualAnchors.high.time || null,
        price: toNumber(manualAnchors.high.price),
      },
      source: 'MANUAL',
      algorithm: 'Manual override',
      confidence: 100,
      reason: 'Admin-selected anchor override is active.',
    }
  }

  if (candles.length < 6) return null
  const price = toNumber(latest?.close, toNumber(candles[candles.length - 1]?.close))
  const atr = toNumber(latest?.atr, price ? Math.max(price * 0.01, 0.25) : 0.25) || 0.25
  const thresholdAbs = Math.max(atr * 0.9, (price || 0) * 0.004, 0.25)
  const lookback = candles.slice(-Math.min(180, candles.length))
  const pivots = detectZigZagPivots(lookback, thresholdAbs)
  const bullish = String(side || '').toUpperCase() === 'LONG'
  const candidatePairs = []

  for (let i = 1; i < pivots.length; i += 1) {
    const prev = pivots[i - 1]
    const current = pivots[i]
    if (bullish && prev.type === 'LOW' && current.type === 'HIGH' && current.time >= prev.time) {
      candidatePairs.push({ low: prev, high: current })
    }
    if (!bullish && prev.type === 'HIGH' && current.type === 'LOW' && current.time >= prev.time) {
      candidatePairs.push({ high: prev, low: current })
    }
  }

  let selected = candidatePairs[candidatePairs.length - 1] || null
  if (!selected) {
    if (bullish) {
      const lowIndex = lookback.reduce((best, row, index) => (row.low < lookback[best].low ? index : best), 0)
      const highSlice = lookback.slice(lowIndex + 1)
      const highIndex = highSlice.length
        ? highSlice.reduce((best, row, index) => (row.high > highSlice[best].high ? index : best), 0)
        : 0
      selected = {
        low: {
          time: lookback[lowIndex].time,
          price: lookback[lowIndex].low,
          volume: lookback[lowIndex].volume,
        },
        high: {
          time: lookback[Math.min(lookback.length - 1, lowIndex + 1 + highIndex)].time,
          price: lookback[Math.min(lookback.length - 1, lowIndex + 1 + highIndex)].high,
          volume: lookback[Math.min(lookback.length - 1, lowIndex + 1 + highIndex)].volume,
        },
      }
    } else {
      const highIndex = lookback.reduce((best, row, index) => (row.high > lookback[best].high ? index : best), 0)
      const lowSlice = lookback.slice(highIndex + 1)
      const lowIndex = lowSlice.length
        ? lowSlice.reduce((best, row, index) => (row.low < lowSlice[best].low ? index : best), 0)
        : 0
      selected = {
        high: {
          time: lookback[highIndex].time,
          price: lookback[highIndex].high,
          volume: lookback[highIndex].volume,
        },
        low: {
          time: lookback[Math.min(lookback.length - 1, highIndex + 1 + lowIndex)].time,
          price: lookback[Math.min(lookback.length - 1, highIndex + 1 + lowIndex)].low,
          volume: lookback[Math.min(lookback.length - 1, highIndex + 1 + lowIndex)].volume,
        },
      }
    }
  }

  if (!selected?.low || !selected?.high) return null
  const range = Math.abs(selected.high.price - selected.low.price)
  if (range <= 0) return null

  const recentVolumes = lookback.slice(-10).map((row) => row.volume)
  const anchorVolumeAvg = average(recentVolumes) || 0
  const lowVolumeBonus = selected.low.volume && anchorVolumeAvg && selected.low.volume >= anchorVolumeAvg ? 10 : 0
  const highVolumeBonus = selected.high.volume && anchorVolumeAvg && selected.high.volume >= anchorVolumeAvg ? 10 : 0
  const displacementAtr = atr > 0 ? range / atr : range / Math.max((price || 1) * 0.01, 1)
  const recencyBonus = Math.min(20, Math.max(0, 20 - Math.floor((lookback.length - Math.max(selected.low.index || 0, selected.high.index || 0)) / 5)))
  const alignmentBonus = bullish
    ? (selected.high.price > price ? 10 : 0) + (toNumber(latest?.ema_fast) > toNumber(latest?.ema_slow) ? 10 : 0)
    : (selected.low.price < price ? 10 : 0) + (toNumber(latest?.ema_fast) < toNumber(latest?.ema_slow) ? 10 : 0)
  const confidence = Math.max(0, Math.min(100, 35 + Math.round(displacementAtr * 12) + lowVolumeBonus + highVolumeBonus + recencyBonus + alignmentBonus))
  const algorithm = 'ATR-adjusted zig-zag swing selection'
  const reason = bullish
    ? `Most recent confirmed swing low-to-high pair with ${displacementAtr.toFixed(2)} ATR displacement${lowVolumeBonus || highVolumeBonus ? ' and above-average anchor volume' : ''}.`
    : `Most recent confirmed swing high-to-low pair with ${displacementAtr.toFixed(2)} ATR displacement${lowVolumeBonus || highVolumeBonus ? ' and above-average anchor volume' : ''}.`

  return {
    direction: bullish ? 'BULLISH' : 'BEARISH',
    low: {
      time: selected.low.time,
      price: toNumber(selected.low.price),
      volume: selected.low.volume || 0,
    },
    high: {
      time: selected.high.time,
      price: toNumber(selected.high.price),
      volume: selected.high.volume || 0,
    },
    source: 'AUTO',
    algorithm,
    confidence,
    reason,
  }
}

function calculateFibLevels(anchor, side) {
  if (!anchor?.low || !anchor?.high) return []
  const bullish = String(side || '').toUpperCase() === 'LONG'
  const low = toNumber(anchor.low.price)
  const high = toNumber(anchor.high.price)
  if (low === null || high === null || high === low) return []
  const range = Math.abs(high - low)
  const base = bullish ? low : high
  const multiplier = bullish ? 1 : -1
  const retracementRatios = [0, 23.6, 38.2, 50, 61.8, 78.6, 100]
  const extensionRatios = [127.2, 161.8, 200]

  const retracements = retracementRatios.map((ratio) => ({
    label: `${ratio.toFixed(1).replace('.0', '')}%`,
    ratio,
    price: bullish
      ? low + (range * (ratio / 100))
      : high - (range * (ratio / 100)),
    kind: ratio === 0 || ratio === 100 ? 'anchor' : 'retracement',
  }))
  const extensions = extensionRatios.map((ratio) => ({
    label: `${ratio.toFixed(1).replace('.0', '')}%`,
    ratio,
    price: bullish
      ? high + (range * ((ratio - 100) / 100))
      : low - (range * ((ratio - 100) / 100)),
    kind: 'extension',
  }))

  return [...retracements, ...extensions].map((level) => ({
    ...level,
    direction: bullish ? 'BULLISH' : 'BEARISH',
    anchor_low: anchor.low,
    anchor_high: anchor.high,
  }))
}

function scoreConfluence(levelPrice, context) {
  const price = toNumber(levelPrice)
  if (price === null) return { score: 0, label: 'WEAK', reasons: [] }

  const tolerance = Math.max(price * 0.003, toNumber(context?.atr, 0) * 0.25, 0.25)
  const candidates = [
    { label: 'VWAP', value: context?.vwap, weight: 2.0 },
    { label: 'EMA 9', value: context?.ema_9 ?? context?.ema_fast, weight: 1.5 },
    { label: 'EMA 21', value: context?.ema_21 ?? context?.ema_slow, weight: 2.0 },
    { label: 'EMA 50', value: context?.ema_50 ?? context?.ema_trend, weight: 1.5 },
    { label: 'EMA 200', value: context?.ema_200, weight: 1.5 },
    { label: 'Previous day high', value: context?.previous_day_high, weight: 1.5 },
    { label: 'Previous day low', value: context?.previous_day_low, weight: 1.5 },
    { label: 'Premarket high', value: context?.premarket_high, weight: 1.0 },
    { label: 'Premarket low', value: context?.premarket_low, weight: 1.0 },
    { label: 'Current session high', value: context?.current_session_high, weight: 1.5 },
    { label: 'Current session low', value: context?.current_session_low, weight: 1.5 },
    { label: 'Opening range high', value: context?.opening_range_high, weight: 1.5 },
    { label: 'Opening range low', value: context?.opening_range_low, weight: 1.5 },
    { label: 'Support', value: context?.support, weight: 2.0 },
    { label: 'Resistance', value: context?.resistance, weight: 2.0 },
    { label: 'Anchored VWAP', value: context?.anchored_vwap, weight: 1.5 },
    { label: 'Round number', value: context?.round_number, weight: 1.0 },
  ]

  const reasons = []
  let score = 0
  candidates.forEach((candidate) => {
    const value = toNumber(candidate.value)
    if (value === null) return
    const distance = Math.abs(price - value)
    if (distance > tolerance) return
    const closeness = 1 - Math.min(1, distance / tolerance)
    const contribution = Math.round(candidate.weight * (1 + closeness))
    score += contribution
    reasons.push(`${candidate.label} is within ${formatPct((distance / price) * 100, 2)}.`)
  })

  if (score >= 8) return { score, label: 'MAJOR CONFLUENCE', reasons }
  if (score >= 5) return { score, label: 'STRONG', reasons }
  if (score >= 3) return { score, label: 'MODERATE', reasons }
  return { score, label: 'WEAK', reasons }
}

function anchorZoneLabel(levels, currentPrice) {
  if (!levels.length || currentPrice === null) return 'Unavailable'
  const sorted = [...levels].sort((a, b) => a.price - b.price)
  for (let i = 0; i < sorted.length - 1; i += 1) {
    const lower = sorted[i]
    const upper = sorted[i + 1]
    if (currentPrice >= lower.price && currentPrice <= upper.price) {
      return `Between ${lower.label} and ${upper.label}`
    }
  }
  if (currentPrice < sorted[0].price) return `Below ${sorted[0].label}`
  return `Above ${sorted[sorted.length - 1].label}`
}

function nearestLevel(levels, currentPrice, direction = 'below') {
  const price = toNumber(currentPrice)
  if (price === null) return null
  const candidates = levels
    .filter((level) => toNumber(level.price) !== null)
    .sort((a, b) => a.price - b.price)
  if (!candidates.length) return null
  if (direction === 'below') {
    const below = candidates.filter((level) => level.price <= price)
    return below.length ? below[below.length - 1] : null
  }
  const above = candidates.filter((level) => level.price >= price)
  return above.length ? above[0] : null
}

function buildTradeMarkers({ position, decision, scan, candles }) {
  const markers = []
  const entryTs = parseApiTimestamp(position?.opening_timestamp_utc || position?.entry_timestamp || scan?.timestamp)
  const exitTs = parseApiTimestamp(position?.closing_timestamp_utc || position?.exit_timestamp)
  if (entryTs) {
    markers.push({
      time: Math.floor(entryTs.getTime() / 1000),
      position: String(position?.direction || scan?.side || '').toUpperCase() === 'SHORT' ? 'aboveBar' : 'belowBar',
      color: '#16c784',
      shape: String(position?.direction || scan?.side || '').toUpperCase() === 'SHORT' ? 'arrowDown' : 'arrowUp',
      text: 'Entry',
    })
  }
  if (exitTs) {
    markers.push({
      time: Math.floor(exitTs.getTime() / 1000),
      position: String(position?.direction || scan?.side || '').toUpperCase() === 'SHORT' ? 'belowBar' : 'aboveBar',
      color: '#ef4444',
      shape: String(position?.direction || scan?.side || '').toUpperCase() === 'SHORT' ? 'arrowUp' : 'arrowDown',
      text: 'Exit',
    })
  }
  if (!entryTs && candles.length) {
    const last = candles[candles.length - 1]
    markers.push({
      time: Number(last.time),
      position: 'belowBar',
      color: '#60a5fa',
      shape: 'circle',
      text: 'Latest',
    })
  }
  return markers
}

function buildStructurePriceLevels({
  sessionBounds,
  anchors,
  fibLevels,
  currentPrice,
  decision,
  position,
  scan,
}) {
  const levels = []
  const push = (price, label, color = '#94a3b8') => {
    const n = toNumber(price)
    if (n === null) return
    levels.push({ price: n, label, color })
  }

  push(sessionBounds.previous_day_high, 'Previous-day high', '#f59e0b')
  push(sessionBounds.previous_day_low, 'Previous-day low', '#38bdf8')
  push(sessionBounds.premarket_high, 'Premarket high', '#a855f7')
  push(sessionBounds.premarket_low, 'Premarket low', '#22c55e')
  push(sessionBounds.current_session_high, 'Current-session high', '#f97316')
  push(sessionBounds.current_session_low, 'Current-session low', '#0ea5e9')
  push(sessionBounds.opening_range_high, 'Opening-range high', '#facc15')
  push(sessionBounds.opening_range_low, 'Opening-range low', '#22c55e')
  push(anchors?.low?.price, anchors?.source === 'MANUAL' ? 'Manual anchor low' : 'Swing low', '#10b981')
  push(anchors?.high?.price, anchors?.source === 'MANUAL' ? 'Manual anchor high' : 'Swing high', '#f43f5e')

  fibLevels.forEach((level) => {
    const score = scoreConfluence(level.price, {
      ...sessionBounds,
      atr: decision?.tradeExplanation?.targets?.basis?.includes('ATR') ? toNumber(scan?.indicators?.atr) : toNumber(scan?.indicators?.atr),
      vwap: toNumber(scan?.indicators?.vwap),
      ema_9: toNumber(scan?.indicators?.ema_fast),
      ema_21: toNumber(scan?.indicators?.ema_slow),
      ema_50: toNumber(scan?.indicators?.ema_trend),
      ema_200: toNumber(scan?.indicators?.ema_200),
      anchored_vwap: toNumber(scan?.indicators?.vwap),
      support: decision?.tradeExplanation?.invalidation?.price,
      resistance: decision?.tradeExplanation?.entry_trigger?.price,
      round_number: Math.round(level.price / 5) * 5,
    })
    levels.push({
      price: level.price,
      label: `${level.label} ${score.label}`,
      color: level.kind === 'extension' ? '#a78bfa' : level.kind === 'anchor' ? '#94a3b8' : '#64748b',
    })
  })

  push(currentPrice, 'Current price', '#ffffff')

  return levels
}

export function buildPositionChartAnalysis({
  indicatorData,
  marketSession,
  scan = null,
  contracts = null,
  position = null,
  backtest = null,
  aiGate = null,
  decision = null,
  moneyFlow = null,
  optionPresentation = null,
  currentUser = null,
  manualAnchors = null,
  side: explicitSide = null,
}) {
  const candles = normalizeCandles(indicatorData)
  const latest = latestIndicator(indicatorData)
  const currentPrice = toNumber(
    position?.underlying_price
    ?? contracts?.underlying_price
    ?? scan?.price
    ?? latest?.close
    ?? candles[candles.length - 1]?.close,
  )
  const side = String(explicitSide || position?.direction || scan?.side || '').toUpperCase()
  const sessionBounds = deriveSessionBounds(candles, marketSession)
  const trend = trendState(latest, currentPrice)
  const regime = marketRegime(latest, currentPrice, trend, candles)
  const anchors = selectAnchorPair(candles, side, latest, manualAnchors)
  const fibLevels = anchors ? calculateFibLevels(anchors, side) : []
  const priceLines = buildStructurePriceLevels({
    sessionBounds,
    anchors,
    fibLevels,
    currentPrice,
    decision,
    position,
    scan,
  })
  const fibScoreRows = fibLevels.map((level) => ({
    ...level,
    confluence: scoreConfluence(level.price, {
      ...sessionBounds,
      atr: toNumber(latest?.atr),
      vwap: toNumber(latest?.vwap),
      ema_9: toNumber(latest?.ema_fast),
      ema_21: toNumber(latest?.ema_slow),
      ema_50: toNumber(latest?.ema_trend),
      ema_200: toNumber(latest?.ema_200),
      anchored_vwap: toNumber(latest?.vwap),
      support: sessionBounds.current_session_low,
      resistance: sessionBounds.current_session_high,
      round_number: Math.round((level.price || 0) / 5) * 5,
    }),
  }))
  const nearestSupport = nearestLevel(
    [
      ...fibScoreRows.filter((level) => level.price <= (currentPrice ?? Number.POSITIVE_INFINITY)),
      { price: sessionBounds.opening_range_low, label: 'Opening-range low', confluence: { label: 'STRUCTURAL' } },
      { price: sessionBounds.current_session_low, label: 'Current-session low', confluence: { label: 'STRUCTURAL' } },
      { price: sessionBounds.previous_day_low, label: 'Previous-day low', confluence: { label: 'STRUCTURAL' } },
    ],
    currentPrice,
    'below',
  )
  const nearestResistance = nearestLevel(
    [
      ...fibScoreRows.filter((level) => level.price >= (currentPrice ?? Number.NEGATIVE_INFINITY)),
      { price: sessionBounds.opening_range_high, label: 'Opening-range high', confluence: { label: 'STRUCTURAL' } },
      { price: sessionBounds.current_session_high, label: 'Current-session high', confluence: { label: 'STRUCTURAL' } },
      { price: sessionBounds.previous_day_high, label: 'Previous-day high', confluence: { label: 'STRUCTURAL' } },
    ],
    currentPrice,
    'above',
  )

  const zoneLabel = anchorZoneLabel(fibScoreRows, currentPrice)
  const zoneConfluence = fibScoreRows.find((level) => zoneLabel.includes(level.label))
  const supportLevel = nearestSupport?.price ?? sessionBounds.current_session_low ?? anchors?.low?.price ?? null
  const resistanceLevel = nearestResistance?.price ?? sessionBounds.current_session_high ?? anchors?.high?.price ?? null
  const thesisLevel = toNumber(decision?.tradeExplanation?.entry_trigger?.price)
    ?? toNumber(position?.entry_underlying_price)
    ?? (side === 'SHORT' ? resistanceLevel : supportLevel)
  const invalidationLevel = toNumber(decision?.tradeExplanation?.invalidation?.price)
    ?? (side === 'SHORT' ? supportLevel : supportLevel)
  const target1 = toNumber(decision?.tradeExplanation?.targets?.target_1)
    ?? (side === 'SHORT' ? supportLevel : resistanceLevel)
  const target2 = toNumber(decision?.tradeExplanation?.targets?.target_2)
    ?? (side === 'SHORT' ? fibScoreRows.find((level) => level.price < (currentPrice ?? Infinity))?.price : fibScoreRows.find((level) => level.price > (currentPrice ?? -Infinity))?.price)
  const target3 = toNumber(decision?.tradeExplanation?.targets?.stretch_target)
    ?? (side === 'SHORT'
      ? fibScoreRows.filter((level) => level.kind === 'extension' && level.price < (currentPrice ?? Infinity))[0]?.price
      : fibScoreRows.filter((level) => level.kind === 'extension' && level.price > (currentPrice ?? -Infinity))[0]?.price)
  const remainingReward = currentPrice !== null && target2 !== null ? Math.abs(target2 - currentPrice) : null
  const remainingRisk = currentPrice !== null && invalidationLevel !== null ? Math.abs(currentPrice - invalidationLevel) : null
  const rewardToRisk = remainingReward !== null && remainingRisk ? Number((remainingReward / remainingRisk).toFixed(2)) : null
  const requiredConfirmation = decision?.tradeExplanation?.entry_trigger?.confirmation_needed
    || (side === 'SHORT'
      ? 'A completed 15-minute close below the trigger with volume above recent average and VWAP rejecting.'
      : 'A completed 15-minute close above the trigger with volume above recent average and VWAP holding.')
  const dataFreshness = classifyFreshness(indicatorData?.timestamp || indicatorData?.last_updated, marketSession)
  const confidenceParts = [
    toNumber(anchors?.confidence, 0) || 0,
    moneyFlow?.confidence === 'HIGH' ? 20 : moneyFlow?.confidence === 'MEDIUM' ? 10 : 0,
    optionPresentation?.labels?.liquidity === 'Excellent' ? 15 : optionPresentation?.labels?.liquidity === 'Good' ? 10 : optionPresentation?.labels?.liquidity === 'Fair' ? 5 : 0,
    dataFreshness === 'LIVE' || dataFreshness === 'PREVIOUS_SESSION' ? 10 : 0,
  ]
  const confidence = Math.max(0, Math.min(100, confidenceParts.reduce((sum, value) => sum + value, 0)))
  const chartStatus = decision?.status || (marketSession?.actionable_live_quotes ? 'WAIT FOR CONFIRMATION' : 'NEXT-SESSION PLANNING')

  return {
    side,
    current_price: currentPrice,
    session_state: marketSession?.session_state || 'UNKNOWN',
    session_note: marketSession?.session_note || '',
    session_label: marketSession?.actionable_live_quotes ? 'Live' : 'Previous session',
    data_freshness: dataFreshness,
    trend,
    market_regime: regime,
    anchors,
    fib_levels: fibScoreRows,
    price_levels: priceLines,
    reference_levels: {
      thesis: thesisLevel,
      invalidation: invalidationLevel,
      target1,
      target2,
      target3,
      support: supportLevel,
      resistance: resistanceLevel,
    },
    trade_markers: buildTradeMarkers({ position, decision, scan, candles }),
    summary: {
      status: chartStatus,
      position_direction: side || 'NEUTRAL',
      trend: trend.label,
      market_regime: regime.label,
      money_flow_alignment: moneyFlow?.alignment || moneyFlow?.classification || 'INSUFFICIENT DATA',
      options_positioning_alignment: moneyFlow?.options_alignment?.classification
        || position?.options_positioning?.classification
        || optionPresentation?.labels?.contract_quality
        || 'INSUFFICIENT DATA',
      current_fibonacci_zone: zoneLabel,
      current_fibonacci_label: zoneConfluence?.confluence?.label || 'WEAK',
      nearest_support: supportLevel,
      nearest_resistance: resistanceLevel,
      thesis_level: thesisLevel,
      invalidation_level: invalidationLevel,
      target_1: target1,
      target_2: target2,
      target_3: target3,
      remaining_reward: remainingReward,
      remaining_risk: remainingRisk,
      reward_to_risk: rewardToRisk,
      required_confirmation: requiredConfirmation,
      data_freshness: dataFreshness,
      confidence,
      fib_algorithm: anchors?.algorithm || 'unavailable',
      fib_reason: anchors?.reason || 'No confirmed swing pair yet.',
      fib_source: anchors?.source || 'unavailable',
      fib_direction: anchors?.direction || 'UNKNOWN',
      fib_confidence: anchors?.confidence || 0,
    },
    warnings: [
      marketSession?.actionable_live_quotes ? null : 'Market closed - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open.',
      anchors ? null : 'A confirmed swing pair could not be selected from the available 15-minute candles.',
    ].filter(Boolean),
  }
}
