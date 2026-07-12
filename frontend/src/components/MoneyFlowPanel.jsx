function value(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '-'
}

function pct(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(digits)}%` : '-'
}

function List({ items }) {
  const rows = Array.isArray(items) ? items.filter(Boolean) : []
  if (!rows.length) return <p className="text-slate-400">-</p>
  return (
    <ul className="space-y-1">
      {rows.map((item, idx) => <li key={`${idx}-${item}`}>{item}</li>)}
    </ul>
  )
}

function tone(score) {
  const n = Number(score)
  if (!Number.isFinite(n)) return 'border-slate-700 bg-panel2 text-slate-200'
  if (n >= 40) return 'border-emerald-800/60 bg-emerald-900/20 text-emerald-200'
  if (n >= 15) return 'border-emerald-700/50 bg-emerald-900/10 text-emerald-200'
  if (n <= -40) return 'border-red-800/60 bg-red-900/20 text-red-200'
  if (n <= -15) return 'border-amber-800/60 bg-amber-900/20 text-amber-200'
  return 'border-slate-700 bg-panel2 text-slate-200'
}

export default function MoneyFlowPanel({ moneyFlow, title = 'Money Flow', compact = false }) {
  if (!moneyFlow) return null
  const priceConfirmation = moneyFlow.price_confirmation || {}
  const vwap = moneyFlow.vwap_behavior || {}
  const tradePressure = moneyFlow.trade_pressure || {}
  const orderBook = moneyFlow.order_book || {}
  const accumulation = moneyFlow.accumulation || {}
  const options = moneyFlow.options_alignment || {}
  const sessionLabel = moneyFlow.session_label || 'Previous session'
  const freshness = moneyFlow.data_freshness || moneyFlow.market_status || '-'

  return (
    <div className={`rounded border p-3 text-sm ${tone(moneyFlow.score)}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-[11px] uppercase text-slate-400">{title}</p>
          <p className="text-base font-semibold">{moneyFlow.classification || 'INSUFFICIENT DATA'}</p>
        </div>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded border border-slate-600 bg-slate-900/40 px-2 py-0.5">Score {value(moneyFlow.score, 1)}</span>
          <span className="rounded border border-slate-600 bg-slate-900/40 px-2 py-0.5">Confidence {moneyFlow.confidence || '-'}</span>
          <span className="rounded border border-slate-600 bg-slate-900/40 px-2 py-0.5">{sessionLabel}</span>
          <span className="rounded border border-slate-600 bg-slate-900/40 px-2 py-0.5">{freshness}</span>
        </div>
      </div>

      <div className="mt-2 text-sm text-slate-200">
        <p>{moneyFlow.position_advice || 'Supports waiting for confirmation.'}</p>
        {moneyFlow.position_aligned !== null && moneyFlow.position_aligned !== undefined && (
          <p className="mt-1 text-xs text-slate-400">
            Position alignment: {moneyFlow.position_aligned ? 'aligned' : 'not aligned'} with current pressure.
          </p>
        )}
      </div>

      {compact ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4 text-xs text-slate-300">
          <div>Price confirmation: <span className="font-semibold">{pct(priceConfirmation.price_change_pct)}</span> | Vol {value(priceConfirmation.volume, 0)} | RVOL {value(priceConfirmation.relative_volume, 2)}</div>
          <div>VWAP: <span className="font-semibold">{vwap.above_vwap === null ? '-' : (vwap.above_vwap ? 'Above' : 'Below')}</span> | Dist {pct(vwap.distance_from_vwap_pct)}</div>
          <div>Options alignment: <span className="font-semibold">{options.classification || '-'}</span> | {options.bias || '-'}</div>
          <div>Trade pressure: <span className="font-semibold">{tradePressure.data_status || 'unavailable'}</span></div>
          <div>Order book: <span className="font-semibold">{orderBook.data_status || 'unavailable'}</span></div>
          <div>Evidence: <span className="font-semibold">{(moneyFlow.evidence_of_buying_pressure || []).length + (moneyFlow.evidence_of_selling_pressure || []).length}</span> signals</div>
        </div>
      ) : (
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Price and volume confirmation</p>
            <div className="space-y-1 text-slate-200">
              <p>Price change: {pct(priceConfirmation.price_change_pct)} ({value(priceConfirmation.price_change)})</p>
              <p>Volume: {value(priceConfirmation.volume, 0)} | Relative volume: {value(priceConfirmation.relative_volume, 2)}</p>
              <p>Dollar volume: {moneyFlow.price_confirmation?.dollar_volume === null || moneyFlow.price_confirmation?.dollar_volume === undefined ? '-' : `$${Number(moneyFlow.price_confirmation.dollar_volume).toFixed(2)}`}</p>
              <p>Volume vs same time of day: {priceConfirmation.volume_vs_same_time_of_day?.ratio !== undefined ? value(priceConfirmation.volume_vs_same_time_of_day?.ratio, 2) : (priceConfirmation.volume_vs_same_time_of_day?.status === 'observed' ? '-' : 'unavailable')}</p>
              <p>Price movement per 1k shares: {value(priceConfirmation.price_movement_per_1k_shares, 4)}</p>
              <p>Rising volume on up candles: {priceConfirmation.rising_volume_on_up_candles === null || priceConfirmation.rising_volume_on_up_candles === undefined ? '-' : String(priceConfirmation.rising_volume_on_up_candles)}</p>
            </div>
          </div>

          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">VWAP behavior</p>
            <div className="space-y-1 text-slate-200">
              <p>Above VWAP: {vwap.above_vwap === null || vwap.above_vwap === undefined ? '-' : String(vwap.above_vwap)}</p>
              <p>VWAP slope: {value(vwap.vwap_slope, 4)}</p>
              <p>Holds / rejections: {value(vwap.holds, 0)} / {value(vwap.rejections, 0)}</p>
              <p>Distance from VWAP: {pct(vwap.distance_from_vwap_pct)}</p>
              <p>Reclaim / lose with volume: {vwap.reclaim_or_lose_with_volume || '-'}</p>
            </div>
          </div>

          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Trade pressure</p>
            {tradePressure.data_status === 'observed' ? (
              <div className="space-y-1 text-slate-200">
                <p>Ask volume: {value(tradePressure.ask_volume, 0)} | Bid volume: {value(tradePressure.bid_volume, 0)}</p>
                <p>Ask / bid share: {pct(tradePressure.ask_volume_pct)} / {pct(tradePressure.bid_volume_pct)}</p>
                <p>Cumulative delta: {value(tradePressure.cumulative_volume_delta, 0)}</p>
                <p>Rolling delta: {value(tradePressure.rolling_volume_delta, 0)}</p>
                <p>Large trade imbalance: {value(tradePressure.large_trade_imbalance, 0)}</p>
                <p>Aggressive buy / sell ratio: {value(tradePressure.aggressive_buy_sell_ratio, 2)}</p>
              </div>
            ) : (
              <div className="space-y-1 text-slate-200">
                <p>Status: {tradePressure.data_status || 'unavailable'}</p>
                <p>{tradePressure.reason || 'Trade-level bid/ask classification was not provided.'}</p>
              </div>
            )}
          </div>

          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Order book</p>
            {orderBook.data_status === 'observed' ? (
              <div className="space-y-1 text-slate-200">
                <p>Displayed bid size: {value(orderBook.total_displayed_bid_size, 0)} | Ask size: {value(orderBook.total_displayed_ask_size, 0)}</p>
                <p>Depth imbalance: {value(orderBook.bid_ask_depth_imbalance, 2)}</p>
                <p>Liquidity near price: {orderBook.liquidity_near_current_price || '-'}</p>
                <p>Bid replenishment: {orderBook.repeated_bid_replenishment === null || orderBook.repeated_bid_replenishment === undefined ? '-' : String(orderBook.repeated_bid_replenishment)}</p>
                <p>Offer replenishment: {orderBook.repeated_offer_replenishment === null || orderBook.repeated_offer_replenishment === undefined ? '-' : String(orderBook.repeated_offer_replenishment)}</p>
                <p>Large resting orders: {orderBook.large_resting_orders || '-'}</p>
                <p>Rapid cancellation: {orderBook.rapid_order_cancellation === null || orderBook.rapid_order_cancellation === undefined ? '-' : String(orderBook.rapid_order_cancellation)}</p>
              </div>
            ) : (
              <div className="space-y-1 text-slate-200">
                <p>Status: {orderBook.data_status || 'unavailable'}</p>
                <p>{orderBook.reason || 'Level II data was not provided.'}</p>
              </div>
            )}
          </div>

          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Accumulation and distribution</p>
            <div className="space-y-1 text-slate-200">
              <p>OBV: {value(accumulation.obv, 0)} | Slope: {value(accumulation.obv_slope, 0)}</p>
              <p>AD line: {value(accumulation.accumulation_distribution_line, 0)} | Slope: {value(accumulation.accumulation_distribution_slope, 0)}</p>
              <p>Chaikin money flow: {value(accumulation.chaikin_money_flow, 4)}</p>
              <p>Money flow index: {value(accumulation.money_flow_index, 2)}</p>
              <p>Anchored VWAP: {value(accumulation.anchored_vwap, 2)}</p>
              <p>Close location in range: {pct(accumulation.close_location_within_range_pct)}</p>
            </div>
          </div>

          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Options alignment</p>
            <div className="space-y-1 text-slate-200">
              <p>Classification: {options.classification || '-'}</p>
              <p>Bias: {options.bias || '-'}</p>
              <p>Bias score: {value(options.bias_score, 0)} | Alignment score: {value(options.alignment_score, 0)}</p>
              <p>Confidence: {options.confidence || '-'}</p>
              <p>Selected expiration: {options.selected_expiration || '-'}</p>
              <p>Baseline comparison: {options.baseline?.comparison || '-'}</p>
            </div>
          </div>

          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Evidence and triggers</p>
            <div className="space-y-2">
              <div>
                <p className="text-xs uppercase text-slate-400">Buying pressure</p>
                <List items={moneyFlow.evidence_of_buying_pressure} />
              </div>
              <div>
                <p className="text-xs uppercase text-slate-400">Selling pressure</p>
                <List items={moneyFlow.evidence_of_selling_pressure} />
              </div>
              <div>
                <p className="text-xs uppercase text-slate-400">Conflicts</p>
                <List items={moneyFlow.conflicting_evidence} />
              </div>
              <div>
                <p className="text-xs uppercase text-slate-400">Confirm direction</p>
                <List items={moneyFlow.what_would_confirm_direction} />
              </div>
              <div>
                <p className="text-xs uppercase text-slate-400">Invalidate direction</p>
                <List items={moneyFlow.what_would_invalidate_direction} />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
