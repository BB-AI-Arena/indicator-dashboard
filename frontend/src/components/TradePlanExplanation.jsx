function money(value) {
  const n = Number(value)
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : '-'
}

function premium(value) {
  const n = Number(value)
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : '-'
}

function Section({ title, children }) {
  return (
    <div>
      <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">{title}</p>
      <div className="text-sm text-slate-200">{children}</div>
    </div>
  )
}

function TextList({ items }) {
  const rows = Array.isArray(items) ? items.filter(Boolean) : []
  if (!rows.length) return <p>-</p>
  return (
    <ul className="space-y-1">
      {rows.map((item, idx) => <li key={`${idx}-${item}`}>{item}</li>)}
    </ul>
  )
}

export default function TradePlanExplanation({ explanation }) {
  if (!explanation) return null

  const entry = explanation.entry_trigger || {}
  const invalidation = explanation.invalidation || {}
  const targets = explanation.targets || {}
  const execution = explanation.option_execution || {}
  const noTrade = explanation.final_decision === 'NO_TRADE'
  const entryAvailable = Number(entry.price) > 0
  const targetsAvailable = Number(targets.target_1) > 0 || Number(targets.target_2) > 0 || Number(targets.stretch_target) > 0
  const executionAvailable = Number(execution.max_reasonable_entry) > 0

  const marketSession = explanation.market_session || null
  const liveSession = marketSession == null ? true : Boolean(marketSession?.actionable_live_quotes)
  const sessionReady = Boolean(marketSession)
  const sessionState = String(marketSession?.session_state || '').toUpperCase()

  return (
    <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
      <div className="mb-3">
        <p className="text-base font-semibold text-slate-100">{!sessionReady || liveSession ? 'Trade Plan Explanation' : 'Next Session Plan'}</p>
        <p className="mt-1 text-slate-300">{explanation.plain_english_summary}</p>
        {sessionReady && marketSession?.session_note && (
          <p className="mt-1 rounded border border-amber-700/60 bg-amber-900/20 p-2 text-xs text-amber-200">
            {marketSession.session_note}
          </p>
        )}
        {sessionReady && sessionState && (
          <p className="mt-1 text-xs uppercase tracking-wide text-slate-400">
            Session state: {sessionState} | Next open: {marketSession?.next_market_open ? new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }).format(new Date(marketSession.next_market_open)) : '-'}
          </p>
        )}
        {Number(explanation.underlying_reference?.price) > 0 && (
          <p className="mt-1 text-xs text-slate-400">
            Underlying reference: {explanation.underlying_reference.label || (liveSession ? 'Live E*TRADE price' : 'Previous session reference')} {money(explanation.underlying_reference.price)}
          </p>
        )}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Section title="Why this passed/failed">
          <p>{explanation.why_passed_or_failed || '-'}</p>
        </Section>

        <Section title="What to watch">
          <TextList items={explanation.watch_for} />
        </Section>

        <Section title="Entry trigger">
          {noTrade ? (
            <p>
              {entryAvailable ? `Reference level: ${money(entry.price)}.` : `${entry.condition || (liveSession ? 'Live E*TRADE price unavailable.' : 'Refresh the next session before defining a trigger.')}`}{' '}
              {entry.confirmation_needed || 'Fresh candle data with volume is required before entry.'}
            </p>
          ) : (
            <>
              <p>{entry.condition || '-'}</p>
              <p className="mt-1 text-slate-400">{entry.confirmation_needed || '-'}</p>
            </>
          )}
        </Section>

        <Section title="Invalidation">
          <p>{invalidation.condition || `Invalidation: ${money(invalidation.price)}`}</p>
        </Section>

        <Section title="Targets">
          {targetsAvailable ? (
            <>
              <p>Target area 1: {money(targets.target_1)}</p>
              <p>Target area 2: {money(targets.target_2)}</p>
              <p>Stretch target area: {money(targets.stretch_target)}</p>
              <p className="mt-1 text-slate-400">Basis: {targets.basis || '-'}</p>
            </>
          ) : (
            <p>Targets are unavailable until a live E*TRADE quote loads.</p>
          )}
        </Section>

        <Section title="Option execution notes">
          {executionAvailable ? (
            <>
              <p>Candidate contract: {execution.candidate_contract || '-'}</p>
              <p>Max reasonable entry: {premium(execution.max_reasonable_entry)}</p>
              <p>Ideal entry zone: {execution.ideal_entry_zone || '-'}</p>
              <p>Take-profit areas: {premium(execution.take_profit_1)} / {premium(execution.take_profit_2)}</p>
              <p>Stop premium: {premium(execution.stop_premium)}</p>
              <p className="mt-1 text-amber-300">{execution.avoid_if || '-'}</p>
            </>
          ) : (
            <p>Option execution notes are unavailable until a fresh live quote is available.</p>
          )}
        </Section>

        <Section title="What would upgrade it">
          <TextList items={explanation.upgrade_conditions} />
        </Section>

        <Section title="What would downgrade it">
          <TextList items={explanation.downgrade_conditions} />
        </Section>

        <Section title="What would cancel it">
          <TextList items={explanation.cancel_conditions} />
        </Section>
      </div>
    </div>
  )
}
