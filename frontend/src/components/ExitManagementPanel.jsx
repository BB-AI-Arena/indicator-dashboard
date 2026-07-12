function money(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : '-'
}

function multiple(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${number >= 0 ? '+' : ''}${number.toFixed(2)}R` : '-'
}

function percent(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${number >= 0 ? '+' : ''}${number.toFixed(1)}%` : '-'
}

function decisionTone(decision) {
  const value = String(decision || '').toUpperCase()
  if (value.includes('CLOSE') || value.includes('INVALID')) return 'border-red-700/70 bg-red-950/30 text-red-200'
  if (value.includes('PARTIAL') || value.includes('PROTECT') || value.includes('MOVE')) return 'border-amber-700/70 bg-amber-950/30 text-amber-200'
  if (value.includes('HOLD')) return 'border-emerald-700/70 bg-emerald-950/30 text-emerald-200'
  return 'border-slate-700 bg-slate-900/40 text-slate-200'
}

export default function ExitManagementPanel({ position, riskManagement, real = false }) {
  const plan = position?.exit_plan || riskManagement?.exit_plan || {}
  const management = position?.exit_management || riskManagement?.exit_management || riskManagement || {}
  const complete = plan.status === 'COMPLETE'
  return (
    <section className="mt-3 rounded border border-cyan-900/60 bg-slate-950/45 p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-wide text-cyan-300">Structure-aware exit plan</p>
          <p className="mt-1 text-xs text-slate-400">Management timeframe: {plan.management_timeframe || '5m'} • Plan: {complete ? 'complete' : 'incomplete'}</p>
        </div>
        <span className={`rounded border px-2 py-1 text-xs font-semibold ${decisionTone(management.decision || management.state)}`}>
          {management.decision || management.state || 'DATA REFRESH REQUIRED'}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
        <div><span className="text-xs text-slate-400">Current R</span><p className="font-semibold">{multiple(management.current_r)}</p></div>
        <div><span className="text-xs text-slate-400">Peak R</span><p className="font-semibold">{multiple(management.peak_r)}</p></div>
        <div><span className="text-xs text-slate-400">Target progress</span><p className="font-semibold">{management.target_progress || '-'}</p></div>
        <div><span className="text-xs text-slate-400">Giveback</span><p className="font-semibold">{multiple(management.profit_giveback_r)} / {percent(management.profit_giveback_pct)}</p></div>
        <div><span className="text-xs text-slate-400">Entry / current</span><p className="font-semibold">{money(management.entry)} / {money(management.current_underlying)}</p></div>
        <div><span className="text-xs text-slate-400">VWAP</span><p className="font-semibold">{management.vwap_status || '-'}</p></div>
        <div><span className="text-xs text-slate-400">Trend</span><p className="font-semibold">{management.trend_status || '-'}</p></div>
        <div><span className="text-xs text-slate-400">Stop</span><p className="font-semibold">{money(management.stop_level)} <span className="text-xs text-slate-400">{management.stop_method || ''}</span></p></div>
      </div>

      <div className="mt-3 grid gap-2 text-xs text-slate-300 md:grid-cols-3">
        <p>Initial invalidation: <strong>{money(plan.structural_invalidation)}</strong></p>
        <p>Target 1 / 2: <strong>{money(plan.target_1)} / {money(plan.target_2)}</strong></p>
        <p>Option mark: <strong>{money(management.current_option_value)}</strong> ({management.option_mark_basis || 'unavailable'})</p>
      </div>
      <p className="mt-3 text-sm text-slate-200">{management.reason || 'No exit condition has been confirmed.'}</p>
      <p className="mt-1 text-xs text-slate-400">Next exit condition: {management.next_exit_condition || plan.vwap_exit_condition || '-'}</p>
      <p className="mt-1 text-xs text-amber-200">Hard truth: {management.hard_truth || 'Exit levels are theoretical until executable data is available.'}</p>
      <p className="mt-1 text-xs text-slate-300">Next action: {management.next_action || '-'}</p>
      {management.warnings?.length ? <p className="mt-2 text-xs font-semibold text-red-200">{management.warnings.join(' • ')}</p> : null}
      <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-400">
        <span>Overnight: <strong className="text-slate-200">{management.overnight_action || '-'}</strong></span>
        <span>5% option trail: <strong className="text-slate-200">{management.mechanical_option_stop == null ? 'inactive' : money(management.mechanical_option_stop)}</strong></span>
        {real && <span className="text-amber-200">Suggested only. Broker order confirmed: {management.broker_order_confirmed ? 'yes' : 'no'}.</span>}
      </div>
    </section>
  )
}
