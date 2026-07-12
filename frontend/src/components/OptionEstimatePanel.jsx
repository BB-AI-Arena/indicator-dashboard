import { formatEasternTime } from '../utils/time'

function money(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : '-'
}

function number(value, digits = 4) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : '-'
}

function label(value) {
  return String(value || '-').replaceAll('_', ' ')
}

export default function OptionEstimatePanel({ estimate, quantity, averageCost, real = false }) {
  if (!estimate) return null
  const actualAvailable = Number.isFinite(Number(estimate.last_actual_option_price))
  const estimatedValue = Number(estimate.estimated_current_value)
  const estimatedPositionValue = Number.isFinite(estimatedValue) && Number.isFinite(Number(quantity)) ? estimatedValue * Number(quantity) * 100 : null
  const estimatedPnl = estimatedPositionValue !== null && Number.isFinite(Number(averageCost)) ? estimatedPositionValue - Number(averageCost) : null
  return (
    <div className="mt-3 rounded border border-amber-800/60 bg-amber-950/20 p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-amber-100">Option Value Estimate</p>
        <span className="badge border border-amber-700/60 bg-amber-950/40 text-amber-200">{label(estimate.quote_state)}</span>
      </div>
      {real && <p className="mt-1 text-xs font-semibold uppercase text-amber-200">Estimated after-hours value — option market closed when applicable.</p>}
      <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <div><span className="text-slate-500">Last actual</span><strong className="ml-2">{actualAvailable ? money(estimate.last_actual_option_price) : '-'}</strong></div>
        <div><span className="text-slate-500">Actual bid / ask</span><strong className="ml-2">{money(estimate.last_actual_bid)} / {money(estimate.last_actual_ask)}</strong></div>
        <div><span className="text-slate-500">Actual midpoint</span><strong className="ml-2">{money(estimate.last_actual_midpoint)}</strong></div>
        <div><span className="text-slate-500">Baseline</span><strong className="ml-2">{label(estimate.baseline_type)}</strong></div>
        <div><span className="text-slate-500">Actual timestamp</span><strong className="ml-2">{formatEasternTime(estimate.last_actual_timestamp || estimate.baseline_timestamp, { seconds: false })}</strong></div>
        <div><span className="text-slate-500">{estimate.quote_state === 'ACTUAL_CURRENT' ? 'Actual current' : 'Estimated current'}</span><strong className="ml-2">{money(estimate.estimated_current_value)}</strong></div>
        <div><span className="text-slate-500">Estimated next open</span><strong className="ml-2">{money(estimate.estimated_next_open_value)}</strong></div>
        <div><span className="text-slate-500">IV -5 / unchanged / +5</span><strong className="ml-2">{money(estimate.iv_down_value)} / {money(estimate.estimated_next_open_value)} / {money(estimate.iv_up_value)}</strong></div>
        <div><span className="text-slate-500">Underlying</span><strong className="ml-2">{money(estimate.latest_underlying_price || estimate.underlying_price)}</strong></div>
      </div>
      <div className="mt-2 grid gap-2 text-xs text-slate-400 sm:grid-cols-2 lg:grid-cols-4">
        <span>Delta {number(estimate.delta)}</span><span>Gamma {number(estimate.gamma)}</span><span>Theta/day {number(estimate.theta)}</span><span>Vega {number(estimate.vega)}</span>
        <span>Greek source: {label(estimate.greek_source)}</span><span>Model: {estimate.pricing_model || '-'}</span><span>Calculated: {formatEasternTime(estimate.calculation_timestamp || estimate.created_at, { seconds: false })}</span><span>Executable: No</span>
      </div>
      {estimate.underlying_scenarios && <p className="mt-2 text-xs text-slate-400">Next-open underlying scenarios: -2% {money(estimate.underlying_scenarios.DOWN_2PCT)} · -1% {money(estimate.underlying_scenarios.DOWN_1PCT)} · unchanged {money(estimate.underlying_scenarios.UNCHANGED)} · +1% {money(estimate.underlying_scenarios.UP_1PCT)} · +2% {money(estimate.underlying_scenarios.UP_2PCT)}</p>}
      {estimatedPositionValue !== null && <p className="mt-2 text-xs text-amber-100">Estimated {real ? 'real' : 'paper'} position value: {money(estimatedPositionValue)} · estimated P/L: {money(estimatedPnl)}. This cannot trigger an order, stop, fill, or realized P/L.</p>}
      {estimate.reason && <p className="mt-2 text-xs text-amber-200">{estimate.reason}</p>}
    </div>
  )
}
