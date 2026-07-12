function percent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  return `${(Number(value) * 100).toFixed(1)}%`
}

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-'
  const number = Number(value)
  return `${number < 0 ? '-' : ''}$${Math.abs(number).toFixed(2)}`
}

function rollingLabel(row) {
  if (!row || !row.resolved) return 'No resolved trades'
  return `${row.wins}W / ${row.losses}L${row.neutral ? ` / ${row.neutral}N` : ''}`
}

export default function RecommendationPerformance({ performance, compact = false }) {
  if (!performance) return null
  const all = performance.all_time || {}
  const rolling = performance.rolling || {}
  const best = performance.best_setup
  const weakest = performance.weakest_setup
  const statCards = [
    ['All-time win rate', percent(all.full_trade_win_rate)],
    ['Last 20', `${rollingLabel(rolling.last_20)} — ${percent(rolling.last_20?.full_trade_win_rate)}`],
    ['Target before invalidation', percent(all.target_before_invalidation_rate)],
    ['Profitable option', percent(all.profitable_option_rate)],
    ['Profit factor', all.profit_factor === null || all.profit_factor === undefined ? '-' : Number(all.profit_factor).toFixed(2)],
    ['Expectancy / triggered trade', money(all.expectancy)],
  ]

  return (
    <section className={`card recommendation-performance ${compact ? 'p-3' : 'p-4'}`}>
      <div className="decision-section-title">
        <div>
          <p className="decision-kicker">Recommendation Performance</p>
          <h3>Running results from immutable recommendation snapshots</h3>
        </div>
        <span className="text-xs text-slate-500">{performance.version}</span>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        {statCards.map(([label, value]) => (
          <div key={label} className="rounded border border-slate-800 bg-slate-950/60 p-3">
            <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
            <p className="mt-1 text-lg font-semibold text-slate-100">{value}</p>
          </div>
        ))}
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        <div className="text-sm text-slate-300">
          <p><span className="text-slate-500">Created:</span> {performance.total_recommendations_created ?? 0}</p>
          <p><span className="text-slate-500">Triggered:</span> {performance.total_recommendations_triggered ?? 0}</p>
          <p><span className="text-slate-500">Resolved:</span> {performance.total_recommendations_resolved ?? 0}</p>
        </div>
        <div className="text-sm text-slate-300">
          <p><span className="text-slate-500">Wins / losses:</span> {performance.wins ?? 0} / {performance.losses ?? 0}</p>
          <p><span className="text-slate-500">Neutral or unresolved:</span> {performance.neutral_or_unresolved ?? 0}</p>
          <p><span className="text-slate-500">Never triggered:</span> {performance.non_triggered_recommendations ?? 0}</p>
        </div>
        <div className="text-sm text-slate-300">
          <p><span className="text-slate-500">Last 10:</span> {rollingLabel(rolling.last_10)} — {percent(rolling.last_10?.full_trade_win_rate)}</p>
          <p><span className="text-slate-500">Last 50:</span> {rollingLabel(rolling.last_50)} — {percent(rolling.last_50?.full_trade_win_rate)}</p>
          <p><span className="text-slate-500">This month:</span> {rollingLabel(rolling.current_month)} — {percent(rolling.current_month?.full_trade_win_rate)}</p>
        </div>
      </div>

      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <div className="rounded border border-emerald-900/50 bg-emerald-950/20 p-3 text-sm text-slate-200">
          <p className="text-xs uppercase tracking-wide text-emerald-300">Best setup</p>
          <p className="mt-1 font-semibold">{best?.value || 'No resolved setup history'}</p>
          {best && <p className="text-slate-400">{best.wins} wins / {best.resolved} resolved • {percent(best.full_trade_win_rate)}</p>}
        </div>
        <div className="rounded border border-amber-900/50 bg-amber-950/20 p-3 text-sm text-slate-200">
          <p className="text-xs uppercase tracking-wide text-amber-300">Weakest setup</p>
          <p className="mt-1 font-semibold">{weakest?.value || 'No resolved setup history'}</p>
          {weakest && <p className="text-slate-400">{weakest.wins} wins / {weakest.resolved} resolved • {percent(weakest.full_trade_win_rate)}</p>}
        </div>
      </div>
      <p className="mt-3 text-xs text-slate-500">
        Win rate counts resolved triggered recommendations only; never-triggered, pre-entry invalidations, active, and unresolved recommendations stay outside the trade denominator.
      </p>
    </section>
  )
}
