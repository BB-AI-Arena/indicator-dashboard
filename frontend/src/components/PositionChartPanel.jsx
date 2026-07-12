import { useMemo, useState } from 'react'
import TickerChart from './TickerChart'
import { buildPositionChartAnalysis } from '../utils/positionChart'
import { formatEasternTime } from '../utils/time'

function money(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `$${n.toFixed(digits)}` : '-'
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

function SummaryItem({ label, value }) {
  return (
    <div>
      <p className="text-[11px] uppercase text-slate-400">{label}</p>
      <p className="font-semibold text-slate-100">{value}</p>
    </div>
  )
}

export default function PositionChartPanel({
  title = '15-Minute Position Chart',
  indicatorData,
  marketSession,
  scan,
  contracts,
  position,
  backtest,
  aiGate,
  decision,
  moneyFlow,
  optionPresentation,
  newsCatalyst,
  currentUser,
  side,
}) {
  const [manualLow, setManualLow] = useState('')
  const [manualHigh, setManualHigh] = useState('')
  const canOverride = currentUser?.role === 'admin'

  const manualAnchors = useMemo(() => {
    const low = Number(manualLow)
    const high = Number(manualHigh)
    if (Number.isFinite(low) && Number.isFinite(high) && low > 0 && high > 0) {
      return {
        low: { price: low },
        high: { price: high },
      }
    }
    return null
  }, [manualLow, manualHigh])

  const analysis = useMemo(() => buildPositionChartAnalysis({
    indicatorData,
    marketSession,
    scan,
    contracts,
    position,
    backtest,
    aiGate,
    decision,
    moneyFlow,
    optionPresentation,
    newsCatalyst,
    currentUser,
    manualAnchors,
    side,
  }), [
    indicatorData,
    marketSession,
    scan,
    contracts,
    position,
    backtest,
    aiGate,
    decision,
    moneyFlow,
    optionPresentation,
    newsCatalyst,
    currentUser,
    manualAnchors,
    side,
  ])

  const confidenceText = `${analysis.summary?.confidence ?? 0}%`
  const anchorSource = analysis.anchors?.source === 'MANUAL' ? 'Manual' : 'Automatic'
  const anchorDirection = analysis.anchors?.direction || 'UNKNOWN'

  return (
    <div className="rounded border border-slate-700 bg-panel2 p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-slate-400">{title}</p>
          <p className="text-base font-semibold text-slate-100">{analysis.summary?.status || 'DATA REFRESH REQUIRED'}</p>
          <p className="text-xs text-slate-400">
            Session: <span className="font-semibold text-slate-200">{analysis.session_state || 'UNKNOWN'}</span>
            {' '}| {analysis.session_note || (analysis.session_label === 'Previous session' ? 'Planning only until the next open.' : 'Live data is actionable.')}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            Current price: <span className="font-semibold text-slate-200">{money(analysis.current_price)}</span>
            {' '}| Entry/reference timestamp: <span className="font-semibold text-slate-200">{formatEasternTime(position?.opening_timestamp_utc || position?.entry_timestamp || scan?.timestamp || marketSession?.current_eastern_timestamp, { seconds: false })}</span>
            {' '}| Entry/reference price: <span className="font-semibold text-slate-200">{money(position?.entry_underlying_price ?? decision?.tradeExplanation?.entry_trigger?.price ?? analysis.summary?.thesis_level)}</span>
          </p>
          {analysis.session_state && (
            <p className="mt-1 text-xs text-slate-400">
              Next open: {marketSession?.next_market_open ? formatEasternTime(marketSession.next_market_open, { seconds: false }) : '-'}
            </p>
          )}
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs text-slate-400">
          <div>Trend: <span className="font-semibold text-slate-200">{analysis.summary?.trend || '-'}</span></div>
          <div>Regime: <span className="font-semibold text-slate-200">{analysis.summary?.market_regime || '-'}</span></div>
          <div>Data: <span className="font-semibold text-slate-200">{analysis.data_freshness || '-'}</span></div>
          <div>Confidence: <span className="font-semibold text-slate-200">{confidenceText}</span></div>
        </div>
      </div>

      {analysis.warnings?.length ? (
        <div className="mt-3 rounded border border-amber-800/60 bg-amber-900/20 p-3 text-sm text-amber-200">
          <List items={analysis.warnings} />
        </div>
      ) : null}

      <div className="mt-3">
      <TickerChart
        indicatorData={indicatorData}
        tradeMarkers={[...(analysis.trade_markers || []), ...((newsCatalyst?.news_markers) || [])]}
        priceLevels={analysis.price_levels}
      />
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <div className="rounded border border-slate-700 bg-slate-900/30 p-3 text-sm text-slate-200">
          <p className="mb-2 text-xs font-semibold uppercase text-slate-400">15-Minute Position Plan</p>
          <div className="grid gap-2 md:grid-cols-2">
            <SummaryItem label="Position direction" value={analysis.summary?.position_direction || side || '-'} />
            <SummaryItem label="15-minute trend" value={analysis.summary?.trend || '-'} />
            <SummaryItem label="Market regime" value={analysis.summary?.market_regime || '-'} />
            <SummaryItem label="Money-flow alignment" value={moneyFlow?.alignment || moneyFlow?.classification || 'INSUFFICIENT DATA'} />
            <SummaryItem label="Options alignment" value={analysis.summary?.options_positioning_alignment || moneyFlow?.options_alignment?.classification || position?.options_positioning?.classification || 'INSUFFICIENT DATA'} />
            <SummaryItem label="Current Fibonacci zone" value={analysis.summary?.current_fibonacci_zone || '-'} />
            <SummaryItem label="Nearest support" value={money(analysis.summary?.nearest_support)} />
            <SummaryItem label="Nearest resistance" value={money(analysis.summary?.nearest_resistance)} />
            <SummaryItem label="Thesis level" value={money(analysis.summary?.thesis_level)} />
            <SummaryItem label="Invalidation level" value={money(analysis.summary?.invalidation_level)} />
            <SummaryItem label="Target 1" value={money(analysis.summary?.target_1)} />
            <SummaryItem label="Target 2" value={money(analysis.summary?.target_2)} />
            <SummaryItem label="Target 3" value={money(analysis.summary?.target_3)} />
            <SummaryItem label="Remaining reward" value={money(analysis.summary?.remaining_reward)} />
            <SummaryItem label="Remaining risk" value={money(analysis.summary?.remaining_risk)} />
            <SummaryItem label="Reward-to-risk" value={analysis.summary?.reward_to_risk === null || analysis.summary?.reward_to_risk === undefined ? '-' : analysis.summary.reward_to_risk.toFixed(2)} />
            <SummaryItem label="Required confirmation" value={analysis.summary?.required_confirmation || '-'} />
            <SummaryItem label="Data freshness" value={analysis.summary?.data_freshness || '-'} />
            <SummaryItem label="Confidence" value={analysis.summary?.confidence === null || analysis.summary?.confidence === undefined ? '-' : `${analysis.summary.confidence}%`} />
          </div>
        </div>

        <div className="rounded border border-slate-700 bg-slate-900/30 p-3 text-sm text-slate-200">
          <p className="mb-2 text-xs font-semibold uppercase text-slate-400">Fibonacci Anchors</p>
          <div className="space-y-2">
            <div className="rounded border border-slate-700 bg-slate-900/40 p-2">
              <p>Source: <span className="font-semibold">{anchorSource}</span></p>
              <p>Direction: <span className="font-semibold">{anchorDirection}</span></p>
              <p>Algorithm: <span className="font-semibold">{analysis.summary?.fib_algorithm || analysis.anchors?.algorithm || '-'}</span></p>
              <p>Confidence: <span className="font-semibold">{analysis.summary?.fib_confidence ?? analysis.anchors?.confidence ?? 0}%</span></p>
              <p>Reason: <span className="font-semibold">{analysis.summary?.fib_reason || analysis.anchors?.reason || '-'}</span></p>
            </div>
            <div className="max-h-64 overflow-auto rounded border border-slate-700 bg-slate-900/40 p-2 text-xs">
              {analysis.fib_levels?.length ? (
                <table className="min-w-full text-left">
                  <thead>
                    <tr className="text-slate-400">
                      <th className="pb-1 pr-2">Level</th>
                      <th className="pb-1 pr-2">Price</th>
                      <th className="pb-1 pr-2">Confluence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {analysis.fib_levels.map((level) => (
                      <tr key={`${level.label}-${level.price}`} className="border-t border-slate-800">
                        <td className="py-1 pr-2 font-semibold">{level.label}</td>
                        <td className="py-1 pr-2">{money(level.price)}</td>
                        <td className="py-1 pr-2">
                          <span className={`rounded border px-2 py-0.5 text-[10px] uppercase ${
                            level.confluence?.label === 'MAJOR CONFLUENCE'
                              ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
                              : level.confluence?.label === 'STRONG'
                                ? 'border-sky-700/60 bg-sky-900/30 text-sky-200'
                                : level.confluence?.label === 'MODERATE'
                                  ? 'border-amber-700/60 bg-amber-900/30 text-amber-200'
                                  : 'border-slate-600 bg-slate-800 text-slate-300'
                          }`}>
                            {level.confluence?.label || 'WEAK'}
                          </span>
                          {level.confluence?.reasons?.length ? (
                            <div className="mt-1 text-slate-400">
                              {level.confluence.reasons.slice(0, 2).join(' ')}
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-slate-400">No confirmed swing pair yet.</p>
              )}
            </div>
          </div>
        </div>
      </div>

      {canOverride && (
        <div className="mt-3 rounded border border-indigo-700/60 bg-indigo-950/30 p-3 text-sm text-indigo-100">
          <p className="text-xs font-semibold uppercase text-indigo-300">Admin anchor override</p>
          <p className="mt-1 text-xs text-indigo-200">Manual anchors replace the automatic swing pair for charting only.</p>
          <div className="mt-2 grid gap-2 md:grid-cols-3">
            <label className="text-xs">
              Low anchor
              <input
                className="mt-1 w-full rounded border border-indigo-800 bg-slate-950 px-2 py-1 text-slate-100"
                value={manualLow}
                onChange={(e) => setManualLow(e.target.value)}
                placeholder="Manual low price"
              />
            </label>
            <label className="text-xs">
              High anchor
              <input
                className="mt-1 w-full rounded border border-indigo-800 bg-slate-950 px-2 py-1 text-slate-100"
                value={manualHigh}
                onChange={(e) => setManualHigh(e.target.value)}
                placeholder="Manual high price"
              />
            </label>
            <div className="flex items-end gap-2">
              <button
                type="button"
                className="rounded border border-indigo-700 px-3 py-1.5 font-semibold text-indigo-100 hover:bg-indigo-900/40"
                onClick={() => {
                  setManualLow('')
                  setManualHigh('')
                }}
              >
                Reset to auto
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
