export function buildTradeGatePayload({ symbol, side, scan, contract, contracts, ratios, backtest, marketSession = null }) {
  const ratioAggregate = ratios?.aggregate && Object.keys(ratios.aggregate).length ? ratios.aggregate : null
  return {
    symbol,
    side,
    market_session: marketSession || null,
    scan: {
      symbol: scan?.symbol,
      side: scan?.side,
      score: scan?.score,
      max_score: scan?.max_score,
      grade: scan?.grade,
      price: scan?.price,
      reasons: scan?.reasons || [],
      warnings: scan?.warnings || [],
      indicators: scan?.indicators || {},
    },
    contract,
    options_sentiment: ratioAggregate || contracts?.options_sentiment || {},
    contract_context: {
      source: contracts?.source || contracts?.provider,
      quote_type: contracts?.quote_type,
      quote_timestamp: contracts?.quote_timestamp,
      timestamp: contracts?.timestamp,
      underlying_price: contracts?.underlying_price,
      filters: contracts?.filters,
      recommended_max_spread_pct: contracts?.recommended_max_spread_pct,
      minimum_volume: contracts?.filters?.min_volume,
    },
    backtest: backtest
      ? {
          confidence: backtest.confidence,
          sample_confidence: backtest.sample_confidence,
          historical_edge: backtest.historical_edge,
          confidence_ok: backtest.confidence_ok,
          occurrences: backtest.occurrences,
          wins: backtest.wins,
          win_rate_pct: backtest.win_rate_pct,
          warning: backtest.warning,
        }
      : null,
  }
}
