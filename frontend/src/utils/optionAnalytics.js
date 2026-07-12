const SHARES_PER_CONTRACT = 100

export function toNumber(value, fallback = null) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

export function formatMoney(value, digits = 2) {
  const n = toNumber(value)
  return n === null ? '-' : `$${n.toFixed(digits)}`
}

export function formatPct(value, digits = 2) {
  const n = toNumber(value)
  return n === null ? '-' : `${n.toFixed(digits)}%`
}

export function currentOptionPremium(contract) {
  const bid = toNumber(contract?.bid)
  const ask = toNumber(contract?.ask)
  const last = toNumber(contract?.last)
  if (bid !== null && ask !== null && bid > 0 && ask > 0) return (bid + ask) / 2
  if (last !== null && last > 0) return last
  if (ask !== null && ask > 0) return ask
  if (bid !== null && bid > 0) return bid
  return null
}

function toDateLike(value, fallback = null) {
  if (value instanceof Date) return value
  const parsed = new Date(value || Date.now())
  if (Number.isNaN(parsed.getTime())) return fallback
  return parsed
}

export function daysToExpiration(expiration, now = new Date()) {
  if (!expiration) return null
  const expiry = new Date(`${String(expiration).slice(0, 10)}T00:00:00Z`)
  if (Number.isNaN(expiry.getTime())) return null
  const reference = toDateLike(now)
  if (!reference) return null
  const diffDays = (expiry.getTime() - reference.getTime()) / (24 * 60 * 60 * 1000)
  return Math.max(0, diffDays)
}

function erf(x) {
  const sign = x < 0 ? -1 : 1
  const absX = Math.abs(x)
  const t = 1 / (1 + 0.3275911 * absX)
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t) * Math.exp(-absX * absX)
  return sign * y
}

function normalCdf(x) {
  return 0.5 * (1 + erf(x / Math.SQRT2))
}

function intrinsicValue(optionType, underlyingPrice, strike) {
  if (optionType === 'PUT') return Math.max(0, strike - underlyingPrice)
  return Math.max(0, underlyingPrice - strike)
}

export function blackScholesApproximation({
  underlyingPrice,
  strike,
  daysToExpiry,
  impliedVolatility,
  optionType,
  rate = 0,
  dividendYield = 0,
}) {
  const S = toNumber(underlyingPrice)
  const K = toNumber(strike)
  const days = toNumber(daysToExpiry)
  const ivInput = toNumber(impliedVolatility)
  if (S === null || K === null || days === null || ivInput === null || S <= 0 || K <= 0) return null

  const sigma = ivInput > 1 ? ivInput / 100 : ivInput
  if (!Number.isFinite(sigma) || sigma <= 0) return null

  const T = Math.max(days, 0) / 365
  if (T <= 0) {
    return intrinsicValue(optionType, S, K)
  }

  const sqrtT = Math.sqrt(T)
  const d1 = (Math.log(S / K) + (rate - dividendYield + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
  const d2 = d1 - sigma * sqrtT
  const discountQ = Math.exp(-dividendYield * T)
  const discountR = Math.exp(-rate * T)

  if (optionType === 'PUT') {
    return K * discountR * normalCdf(-d2) - S * discountQ * normalCdf(-d1)
  }
  return S * discountQ * normalCdf(d1) - K * discountR * normalCdf(d2)
}

export function probabilityItmEstimate({ underlyingPrice, strike, daysToExpiry, impliedVolatility, optionType }) {
  const S = toNumber(underlyingPrice)
  const K = toNumber(strike)
  const days = toNumber(daysToExpiry)
  const ivInput = toNumber(impliedVolatility)
  if (S === null || K === null || days === null || ivInput === null || S <= 0 || K <= 0) return null
  const sigma = ivInput > 1 ? ivInput / 100 : ivInput
  if (!Number.isFinite(sigma) || sigma <= 0) return null
  const T = Math.max(days, 0) / 365
  if (T <= 0) return null
  const sqrtT = Math.sqrt(T)
  const d1 = (Math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
  const d2 = d1 - sigma * sqrtT
  const probability = optionType === 'PUT' ? normalCdf(-d2) : normalCdf(d2)
  return Math.max(0, Math.min(100, probability * 100))
}

export function describeGreek(name, value) {
  const n = toNumber(value)
  if (n === null) return { label: '-', description: 'Unavailable.' }

  if (name === 'delta') {
    return {
      label: n.toFixed(2),
      description: `Expected to gain or lose about ${formatMoney(n * SHARES_PER_CONTRACT)} per contract for a $1 move in the underlying, before other factors.`,
    }
  }
  if (name === 'gamma') {
    return {
      label: n.toFixed(3),
      description: `Delta changes by about ${n.toFixed(3)} for each $1 move in the underlying.`,
    }
  }
  if (name === 'theta') {
    return {
      label: n.toFixed(2),
      description: `Estimated time decay is about ${formatMoney(Math.abs(n) * SHARES_PER_CONTRACT)} per contract per day.`,
    }
  }
  if (name === 'vega') {
    return {
      label: n.toFixed(2),
      description: `Estimated option value changes about ${formatMoney(Math.abs(n) * SHARES_PER_CONTRACT)} per contract for a one-point change in implied volatility.`,
    }
  }
  if (name === 'iv') {
    return {
      label: formatPct(n > 1 ? n : n * 100),
      description: 'Implied volatility used for the scenario estimates.',
    }
  }

  return {
    label: String(n),
    description: 'Unavailable.',
  }
}

export function classifyDirectionalSensitivity(delta) {
  const n = Math.abs(toNumber(delta, 0) || 0)
  if (n < 0.35) return 'Low'
  if (n < 0.6) return 'Moderate'
  return 'High'
}

export function classifyGammaRisk(gamma) {
  const n = Math.abs(toNumber(gamma, 0) || 0)
  if (n < 0.02) return 'Low'
  if (n < 0.05) return 'Moderate'
  return 'High'
}

export function classifyThetaRisk(theta) {
  const n = Math.abs(toNumber(theta, 0) || 0)
  if (n < 0.05) return 'Low'
  if (n < 0.15) return 'Moderate'
  return 'High'
}

export function classifyIvSensitivity(vega) {
  const n = Math.abs(toNumber(vega, 0) || 0)
  if (n < 0.05) return 'Low'
  if (n < 0.15) return 'Moderate'
  return 'High'
}

export function classifyLiquidity({ spreadPct, volume, openInterest }) {
  const spread = toNumber(spreadPct)
  const vol = toNumber(volume)
  const oi = toNumber(openInterest)
  if (spread === null || vol === null || oi === null) return 'Fair'
  if (spread > 8 || vol < 25 || oi < 25) return 'Poor'
  if (spread > 5 || vol < 100 || oi < 100) return 'Fair'
  if (spread > 2.5 || vol < 500 || oi < 500) return 'Good'
  return 'Excellent'
}

export function classifyContractQuality({
  directional,
  gammaRisk,
  thetaRisk,
  ivSensitivity,
  liquidity,
  dte,
  quoteType,
  quoteStale,
  expectedTypeMatch = true,
}) {
  if (!expectedTypeMatch) return 'Avoid'
  if (quoteStale || ['CLOSING', 'DELAYED', 'SANDBOX'].includes(String(quoteType || '').toUpperCase())) return 'Avoid'
  if (dte !== null && dte <= 1) return 'Avoid'
  if (liquidity === 'Poor' || thetaRisk === 'High') return 'Weak'
  if (liquidity === 'Excellent' && directional !== 'Low' && gammaRisk !== 'High') return 'Strong'
  if (liquidity === 'Good' && directional !== 'Low') return 'Acceptable'
  return 'Weak'
}

export function optionUnderlyingBreakEven({ contractType, strike, premium }) {
  const K = toNumber(strike)
  const p = toNumber(premium)
  if (K === null || p === null) return null
  if (contractType === 'PUT') return K - p
  return K + p
}

export function buildOptionScenario({
  label,
  contractType,
  strike,
  underlyingPrice,
  currentPremium,
  daysToExpiry,
  impliedVolatility,
  underlyingAssumption,
  dayShift = 0,
  ivShift = 0,
  pricingTimestamp,
  marketSession,
}) {
  const premium = currentPremium
  const scenarioPrice = blackScholesApproximation({
    underlyingPrice: underlyingAssumption,
    strike,
    daysToExpiry: Math.max(0, toNumber(daysToExpiry, 0) - dayShift),
    impliedVolatility: (() => {
      const iv = toNumber(impliedVolatility)
      if (iv === null) return null
      const normalized = iv > 1 ? iv / 100 : iv
      return Math.max(0.0001, normalized + (ivShift / 100))
    })(),
    optionType: contractType,
  })

  const pctChange = premium !== null && premium > 0 && scenarioPrice !== null
    ? ((scenarioPrice - premium) / premium) * 100
    : null
  const limiters = [
    'Black-Scholes approximation used for direction and timing only; American-style early exercise is not modeled.',
  ]
  if (marketSession && !marketSession.actionable_live_quotes) {
    limiters.push('Quote is from the previous session and must be refreshed after the next open.')
  }
  return {
    label,
    estimated_option_price: scenarioPrice === null ? null : Number(scenarioPrice.toFixed(2)),
    estimated_pct_change: pctChange === null ? null : Number(pctChange.toFixed(2)),
    underlying_price_assumption: underlyingAssumption === null ? null : Number(underlyingAssumption.toFixed(2)),
    time_assumption: dayShift === 0 ? 'Same session / same expiration window' : `${dayShift} trading day${dayShift === 1 ? '' : 's'} later`,
    implied_volatility_assumption: ivShift === 0
      ? 'Unchanged'
      : ivShift > 0
        ? `Up ${ivShift} points`
        : `Down ${Math.abs(ivShift)} points`,
    pricing_timestamp: toDateLike(pricingTimestamp, new Date())?.toISOString() || new Date().toISOString(),
    confidence: scenarioPrice === null ? 'LOW' : (marketSession?.actionable_live_quotes ? 'HIGH' : 'MEDIUM'),
    model: 'Black-Scholes approximation',
    limitations: limiters,
    current_premium: premium === null ? null : Number(premium.toFixed(2)),
    breakeven_underlying: premium === null ? null : Number(optionUnderlyingBreakEven({ contractType, strike, premium }).toFixed(2)),
  }
}

export function buildOptionPresentation({ contract, marketSession = null, pricingTimestamp = new Date(), side = null, referenceLevels = {} }) {
  const contractType = String(contract?.type || '').toUpperCase()
  const strike = toNumber(contract?.strike)
  const currentPremium = currentOptionPremium(contract)
  const dte = daysToExpiration(contract?.expiration, pricingTimestamp)
  const underlyingPrice = toNumber(contract?.underlying_price)
  const iv = toNumber(contract?.implied_volatility)
  const delta = toNumber(contract?.delta)
  const gamma = toNumber(contract?.gamma)
  const theta = toNumber(contract?.theta)
  const vega = toNumber(contract?.vega)
  const spreadPct = toNumber(contract?.spread_percentage)
  const volume = toNumber(contract?.volume)
  const openInterest = toNumber(contract?.open_interest)
  const quoteType = String(contract?.quote_type || '').toUpperCase()
  const quoteStale = Boolean(contract?.quote_stale)
  const actionable = marketSession?.actionable_live_quotes !== false
  const expectedType = String(side || '').toUpperCase() === 'LONG' ? 'CALL' : String(side || '').toUpperCase() === 'SHORT' ? 'PUT' : ''
  const expectedTypeMatch = expectedType ? contractType === expectedType : true

  const directionalSensitivity = classifyDirectionalSensitivity(delta)
  const gammaRisk = classifyGammaRisk(gamma)
  const thetaRisk = classifyThetaRisk(theta)
  const ivSensitivity = classifyIvSensitivity(vega)
  const liquidity = classifyLiquidity({ spreadPct, volume, openInterest })
  const quality = classifyContractQuality({
    directional: directionalSensitivity,
    gammaRisk,
    thetaRisk,
    ivSensitivity,
    liquidity,
    dte,
    quoteType,
    quoteStale,
    expectedTypeMatch,
  })

  const liveExecutionWarnings = []
  const structuralWarnings = []
  const positionRiskWarnings = []

  if (expectedType && !expectedTypeMatch) {
    structuralWarnings.push(`${side} bias requires a ${expectedType}; candidate is ${contractType || 'UNKNOWN'}.`)
  }
  if (actionable) {
    if (spreadPct !== null && spreadPct > 5) liveExecutionWarnings.push(`Spread is above 5% (${spreadPct.toFixed(2)}%).`)
    if (volume !== null && volume < 100) liveExecutionWarnings.push(`Volume is below 100 (${volume}).`)
    if (quoteType && ['CLOSING', 'DELAYED', 'SANDBOX'].includes(quoteType)) liveExecutionWarnings.push(`Quote type is ${quoteType}.`)
    if (quoteStale) liveExecutionWarnings.push('Quote is stale.')
  }
  if (!actionable) {
    structuralWarnings.push('Market closed - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open.')
  }
  if (openInterest !== null && openInterest < 100) structuralWarnings.push(`Open interest is low (${openInterest}).`)
  if (dte !== null && dte <= 3) structuralWarnings.push(`Very low DTE (${dte.toFixed(1)} days) increases decay and gamma risk.`)
  if (theta !== null && Math.abs(theta) > 0.15) structuralWarnings.push(`Theta is high at ${theta.toFixed(2)} per share per day.`)
  if (iv !== null && iv > 1.0) structuralWarnings.push(`Implied volatility is elevated at ${(iv > 1 ? iv : iv * 100).toFixed(2)}.`)

  if (quoteStale || quoteType === 'CLOSING' || quoteType === 'DELAYED' || quoteType === 'SANDBOX') {
    positionRiskWarnings.push('Quote refresh is required after the next open before treating this as actionable.')
  }

  const currentPrice = currentPremium
  const pricingOptions = {
    contractType,
    strike,
    underlyingPrice,
    currentPremium,
    daysToExpiry: dte,
    impliedVolatility: iv,
    marketSession,
    pricingTimestamp,
  }
  const scenarios = [
    { label: 'Stop', underlyingAssumption: null },
    { label: 'Breakeven', underlyingAssumption: optionUnderlyingBreakEven({ contractType, strike, premium: currentPremium }) },
    { label: 'Target 1', underlyingAssumption: null },
    { label: 'Target 2', underlyingAssumption: null },
    { label: 'Invalidation', underlyingAssumption: null },
    { label: 'Unchanged after 1 day', underlyingAssumption: underlyingPrice, dayShift: 1, ivShift: 0 },
    { label: 'Unchanged after 3 trading days', underlyingAssumption: underlyingPrice, dayShift: 3, ivShift: 0 },
    { label: 'IV down 5 points', underlyingAssumption: underlyingPrice, dayShift: 0, ivShift: -5 },
    { label: 'IV unchanged', underlyingAssumption: underlyingPrice, dayShift: 0, ivShift: 0 },
    { label: 'IV up 5 points', underlyingAssumption: underlyingPrice, dayShift: 0, ivShift: 5 },
  ]

  return {
    contract_symbol: contract?.contract_symbol || '-',
    contract_type: contractType || '-',
    strike,
    expiration: contract?.expiration || null,
    dte: dte === null ? null : Number(dte.toFixed(1)),
    moneyness: contract?.moneyness || null,
    intrinsic_or_extrinsic: strike !== null && underlyingPrice !== null && currentPremium !== null
      ? (Math.abs(underlyingPrice - strike) > currentPremium ? 'Intrinsic-heavy' : 'Extrinsic-heavy')
      : 'Unavailable',
    in_the_money_label: (() => {
      if (strike === null || underlyingPrice === null) return 'Unavailable'
      if (Math.abs(underlyingPrice - strike) <= (underlyingPrice * 0.01)) return 'ATM'
      if (contractType === 'PUT') return strike > underlyingPrice ? 'ITM' : 'OTM'
      return strike < underlyingPrice ? 'ITM' : 'OTM'
    })(),
    quote_session_label: marketSession == null ? 'Loading' : (marketSession?.actionable_live_quotes ? 'Live' : 'Previous session'),
    current_premium: currentPrice === null ? null : Number(currentPrice.toFixed(2)),
    bid: toNumber(contract?.bid),
    ask: toNumber(contract?.ask),
    last: toNumber(contract?.last),
    spread_dollars: spreadPct === null || currentPremium === null ? null : Number(Math.abs((toNumber(contract?.ask, 0) || 0) - (toNumber(contract?.bid, 0) || 0)).toFixed(2)),
    spread_percentage: spreadPct === null ? null : Number(spreadPct.toFixed(2)),
    volume,
    open_interest: openInterest,
    implied_volatility: iv,
    greeks: {
      delta: describeGreek('delta', delta),
      gamma: describeGreek('gamma', gamma),
      theta: describeGreek('theta', theta),
      vega: describeGreek('vega', vega),
      probability_itm: probabilityItmEstimate({
        underlyingPrice,
        strike,
        daysToExpiry: dte,
        impliedVolatility: iv,
        optionType: contractType,
      }),
    },
    labels: {
      directional_sensitivity: directionalSensitivity,
      gamma_risk: gammaRisk,
      time_decay_risk: thetaRisk,
      iv_sensitivity: ivSensitivity,
      liquidity,
      contract_quality: quality,
    },
    live_execution_warnings: liveExecutionWarnings,
    structural_warnings: structuralWarnings,
    position_risk_warnings: positionRiskWarnings,
    scenarios: scenarios.map((scenario) => {
      if (scenario.label === 'Stop' || scenario.label === 'Target 1' || scenario.label === 'Target 2' || scenario.label === 'Invalidation') {
        const level = scenario.label === 'Stop'
          ? referenceLevels.stop
          : scenario.label === 'Target 1'
            ? referenceLevels.target1
            : scenario.label === 'Target 2'
              ? referenceLevels.target2
              : referenceLevels.invalidation
        return buildOptionScenario({
          label: scenario.label,
          contractType,
          strike,
          underlyingPrice,
          currentPremium,
          daysToExpiry: dte,
          impliedVolatility: iv,
          underlyingAssumption: level === null || level === undefined ? null : toNumber(level),
          pricingTimestamp,
          marketSession,
        })
      }
      if (scenario.label === 'Breakeven') {
        return {
          label: 'Breakeven',
          estimated_option_price: currentPremium === null ? null : Number(currentPremium.toFixed(2)),
          estimated_pct_change: 0,
          underlying_price_assumption: scenario.underlyingAssumption === null ? null : Number(scenario.underlyingAssumption.toFixed(2)),
          time_assumption: 'At expiration',
          implied_volatility_assumption: 'Unchanged',
          pricing_timestamp: toDateLike(pricingTimestamp, new Date())?.toISOString() || new Date().toISOString(),
          confidence: currentPremium === null ? 'LOW' : (marketSession?.actionable_live_quotes ? 'HIGH' : 'MEDIUM'),
          model: 'Break-even reference',
          limitations: ['Break-even at expiration is an arithmetic reference, not a live executable price.'],
          current_premium: currentPremium === null ? null : Number(currentPremium.toFixed(2)),
          breakeven_underlying: scenario.underlyingAssumption === null ? null : Number(scenario.underlyingAssumption.toFixed(2)),
        }
      }
      return buildOptionScenario({
        label: scenario.label,
        contractType,
        strike,
        underlyingPrice,
        currentPremium,
        daysToExpiry: dte,
        impliedVolatility: iv,
        underlyingAssumption: scenario.underlyingAssumption ?? underlyingPrice,
        dayShift: scenario.dayShift || 0,
        ivShift: scenario.ivShift || 0,
        pricingTimestamp,
        marketSession,
      })
    }),
    current_premium_basis: toNumber(contract?.bid) !== null && toNumber(contract?.ask) !== null
      ? 'midpoint'
      : (toNumber(contract?.last) !== null ? 'last' : 'bid/ask unavailable'),
  }
}

export function buildPositionDecision({ position, marketSession = null }) {
  const quoteStale = Boolean(position?.quote_stale)
  const quoteType = String(position?.quote_type || '').toUpperCase()
  const dte = toNumber(position?.days_to_expiration)
  const unrealizedPct = toNumber(position?.unrealized_pnl_pct)
  const theta = toNumber(position?.theta)
  const delta = toNumber(position?.delta)
  const vega = toNumber(position?.vega)
  const strike = toNumber(position?.strike)
  const underlyingPrice = toNumber(position?.underlying_price ?? position?.underlying_quote?.price)
  const currentPositionValue = toNumber(position?.market_value)
  const averageCost = toNumber(position?.cost_basis)
  const unrealizedPnl = toNumber(position?.unrealized_pnl)
  const quantity = Math.max(1, Math.abs(toNumber(position?.quantity, 1) || 1))
  const direction = String(position?.direction || '').toUpperCase()
  const contractType = String(position?.contract_type || '').toUpperCase()
  const actionable = marketSession?.actionable_live_quotes !== false
  const contractCredit = averageCost !== null ? Math.abs(averageCost) / (quantity * SHARES_PER_CONTRACT) : null
  const breakevenAtExpiration = (() => {
    if (strike === null || contractCredit === null) return null
    if (contractType === 'PUT') return direction === 'SHORT' ? strike - contractCredit : strike - contractCredit
    return direction === 'SHORT' ? strike + contractCredit : strike + contractCredit
  })()
  const thetaCostPerDay = theta !== null ? Math.abs(theta) * SHARES_PER_CONTRACT * quantity : null
  const underlyingMoveToOffsetTheta = delta !== null && delta !== 0 && theta !== null
    ? Math.abs(theta) / Math.abs(delta)
    : null

  if (quoteStale && actionable) {
    return {
      status: 'DATA REFRESH REQUIRED',
      reasons_to_continue_holding: ['Refresh the quote before making a live execution decision.'],
      reasons_to_reduce_or_close: ['Live quote is stale, so the current mark may be misleading.'],
      exact_conditions: ['Refresh after the next market open or during the current live session.'],
    }
  }

  const positivePnL = unrealizedPct !== null && unrealizedPct > 0
  const strongPnL = unrealizedPct !== null && unrealizedPct >= 50
  const losing = unrealizedPct !== null && unrealizedPct <= -25
  const lowDte = dte !== null && dte <= 3
  const highTheta = theta !== null && Math.abs(theta) > 0.15
  const ivDriven = vega !== null && Math.abs(vega) > 0.15

  let status = 'HOLDING AS PLANNED'
  if (strongPnL) status = 'PROFIT PROTECTION'
  if (lowDte && positivePnL) status = 'CONSIDER ROLLING'
  if (lowDte && !positivePnL) status = 'EXIT CONDITION APPROACHING'
  if (losing && lowDte) status = 'THESIS INVALIDATED'
  if (highTheta && positivePnL) status = 'CONSIDER REDUCING'
  if (ivDriven && !positivePnL && actionable) status = 'EXIT CONDITION APPROACHING'

  const reasons_to_continue_holding = []
  const reasons_to_reduce_or_close = []
  const exact_conditions = []

  if (positivePnL) reasons_to_continue_holding.push('Position is still profitable and the thesis has not broken yet.')
  if (!lowDte) reasons_to_continue_holding.push('There is still enough time for the move to work without immediate decay pressure.')
  if (highTheta) reasons_to_continue_holding.push('Time decay is not yet overwhelming the trade if the underlying keeps moving in the intended direction.')
  if (lowDte) reasons_to_continue_holding.push('Only hold if the move is already working and you can protect gains quickly.')

  if (quoteType === 'CLOSING' || quoteType === 'DELAYED' || quoteType === 'SANDBOX') {
    reasons_to_reduce_or_close.push(`Quote type is ${quoteType}, so the snapshot is not live-tradable.`)
  }
  if (quoteStale && !actionable) {
    reasons_to_reduce_or_close.push('Quote is previous-session data and must be refreshed after the next open.')
  }
  if (losing) reasons_to_reduce_or_close.push('The position is losing and the remaining edge is shrinking.')
  if (lowDte) reasons_to_reduce_or_close.push('DTE is very low, so theta and gamma risk are starting to dominate.')
  if (ivDriven) reasons_to_reduce_or_close.push('The position is relying heavily on implied volatility rather than directional movement.')

  exact_conditions.push('Continue only while the underlying stays on the correct side of the thesis level and the quote quality remains acceptable.')
  if (direction === 'LONG') {
    exact_conditions.push('For a long setup, keep the underlying above the level that supports the original trend and avoid letting a failed reclaim turn into a hold-and-hope trade.')
  } else if (direction === 'SHORT') {
    exact_conditions.push('For a short setup, keep the underlying below the level that preserves the original edge and avoid giving back the premium through drift.')
  }
  if (lowDte) exact_conditions.push('If expiration is close and the move is not already paying, reduce or roll instead of waiting for a miracle.')

  return {
    status,
    reasons_to_continue_holding,
    reasons_to_reduce_or_close,
    exact_conditions,
    thesis_valid: !losing,
    relying_on_time_or_iv: Boolean(highTheta || ivDriven),
    quote_session_label: marketSession == null ? 'Loading' : (marketSession?.actionable_live_quotes ? 'Live' : 'Previous session'),
    current_position_value: currentPositionValue,
    average_cost: averageCost,
    unrealized_pnl: unrealizedPnl,
    return_pct: unrealizedPct,
    current_underlying_price: underlyingPrice,
    breakeven_at_expiration: breakevenAtExpiration,
    days_remaining: dte,
    theta_cost_per_day: thetaCostPerDay,
    underlying_move_to_offset_theta: underlyingMoveToOffsetTheta,
  }
}
