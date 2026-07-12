function money(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `$${n.toFixed(digits)}` : '-'
}

function pct(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(digits)}%` : '-'
}

function timeLabel(value) {
  if (!value) return '-'
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      timeZoneName: 'short',
    }).format(new Date(value))
  } catch {
    return String(value)
  }
}

function listRows(items) {
  const rows = Array.isArray(items) ? items.filter(Boolean) : []
  if (!rows.length) return <p className="text-slate-400">-</p>
  return (
    <ul className="space-y-1">
      {rows.map((item, idx) => <li key={`${idx}-${item}`}>{item}</li>)}
    </ul>
  )
}

function tone(label) {
  const text = String(label || '').toUpperCase()
  if (text.includes('SUPPORTS')) return 'border-emerald-700/60 bg-emerald-900/20 text-emerald-200'
  if (text.includes('CONFLICTS') || text.includes('INVALIDATED')) return 'border-red-700/60 bg-red-900/20 text-red-200'
  if (text.includes('PARTIALLY')) return 'border-amber-700/60 bg-amber-900/20 text-amber-200'
  if (text.includes('WAIT')) return 'border-sky-700/60 bg-sky-900/20 text-sky-200'
  return 'border-slate-700 bg-panel2 text-slate-200'
}

export default function NewsCatalystPanel({ newsCatalyst, title = 'News and Catalyst Impact' }) {
  if (!newsCatalyst) return null

  const summary = newsCatalyst.summary || {}
  const mostRelevant = summary.most_relevant_event || null
  const events = Array.isArray(newsCatalyst.events) ? newsCatalyst.events : []
  const upcoming = Array.isArray(newsCatalyst.upcoming_catalysts) ? newsCatalyst.upcoming_catalysts : []
  const sourceList = Array.isArray(summary.source_list) ? summary.source_list : Array.isArray(newsCatalyst.source_list) ? newsCatalyst.source_list : []
  const reaction = summary.actual_share_price_reaction || mostRelevant?.actual_share_price_reaction || {}
  const marketAdjusted = summary.market_adjusted_reaction || reaction.market_adjusted || {}
  const sessionNote = newsCatalyst.session_note || ''

  return (
    <details className={`rounded border p-3 text-sm ${tone(summary.position_impact || newsCatalyst.impact_label)}`} open>
      <summary className="cursor-pointer list-none">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-[11px] uppercase text-slate-400">{title}</p>
            <p className="text-base font-semibold">{summary.headline || 'No material recent catalyst found'}</p>
            <p className="mt-1 text-xs text-slate-400">
              Impact: <span className="font-semibold text-slate-100">{summary.position_impact || newsCatalyst.impact_label || 'INSUFFICIENT DATA'}</span>
              {' '}| Confidence: <span className="font-semibold text-slate-100">{summary.data_confidence || newsCatalyst.confidence || '-'}</span>
              {' '}| Session: <span className="font-semibold text-slate-100">{newsCatalyst.market_session || '-'}</span>
            </p>
          </div>
          <div className="text-right text-xs text-slate-400">
            <div>Last refresh: <span className="font-semibold text-slate-200">{timeLabel(newsCatalyst.last_news_refresh)}</span></div>
            <div>Latest event: <span className="font-semibold text-slate-200">{timeLabel(newsCatalyst.latest_relevant_event_timestamp)}</span></div>
            {newsCatalyst.data_freshness ? <div>Data: <span className="font-semibold text-slate-200">{newsCatalyst.data_freshness}</span></div> : null}
          </div>
        </div>
      </summary>

      {sessionNote ? (
        <div className="mt-3 rounded border border-slate-700 bg-slate-900/30 p-3 text-slate-200">
          {sessionNote}
        </div>
      ) : null}

      {mostRelevant ? (
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="text-[11px] uppercase text-slate-400">Most relevant event</p>
            <p className="mt-1 font-semibold text-slate-100">{mostRelevant.headline || '-'}</p>
            <p className="text-xs text-slate-400">
              {mostRelevant.event_category || '-'} | {timeLabel(mostRelevant.publication_timestamp)} | {mostRelevant.source || '-'}
            </p>
            <p className="mt-2 text-slate-200">{mostRelevant.why_it_matters || summary.why_it_matters || '-'}</p>
            {mostRelevant.url ? (
              <a className="mt-2 inline-block text-xs text-sky-300 underline" href={mostRelevant.url} target="_blank" rel="noreferrer">
                Source link
              </a>
            ) : null}
          </div>
          <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
            <p className="text-[11px] uppercase text-slate-400">Reaction</p>
            <div className="mt-1 grid gap-1 text-xs text-slate-200 sm:grid-cols-2">
              <div>Price before publication: <span className="font-semibold">{money(reaction.price_before_publication)}</span></div>
              <div>First tradable price: <span className="font-semibold">{money(reaction.first_tradable_price_after_publication)}</span></div>
              <div>Gap: <span className="font-semibold">{pct(reaction.gap_percentage)}</span></div>
              <div>15m / 30m / 1h: <span className="font-semibold">{pct(reaction.return_15m_pct)} / {pct(reaction.return_30m_pct)} / {pct(reaction.return_1h_pct)}</span></div>
              <div>Close-to-close: <span className="font-semibold">{pct(reaction.close_to_close_pct)}</span></div>
              <div>Next session: <span className="font-semibold">{pct(reaction.next_session_return_pct)}</span></div>
              <div>Volume vs normal: <span className="font-semibold">{reaction.relative_volume === null || reaction.relative_volume === undefined ? '-' : `${Number(reaction.relative_volume).toFixed(2)}x`}</span></div>
              <div>Classification: <span className="font-semibold">{mostRelevant.news_reaction_classification || '-'}</span></div>
            </div>
            <p className="mt-2 text-xs text-slate-400">
              Market-adjusted: ticker minus SPY {pct(marketAdjusted.ticker_return_minus_spy_return)} | ticker minus QQQ {pct(marketAdjusted.ticker_return_minus_qqq_return)}
            </p>
            <p className="mt-1 text-xs text-slate-400">Options response: {summary.options_response || 'unavailable'}</p>
          </div>
        </div>
      ) : null}

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
          <p className="text-[11px] uppercase text-slate-400">Position impact</p>
          <p className="mt-1 font-semibold text-slate-100">{summary.position_impact || newsCatalyst.impact_label || 'INSUFFICIENT DATA'}</p>
          <p className="mt-1 text-slate-200">{summary.plain_english_summary || summary.why_it_matters || '-'}</p>
          <p className="mt-2 text-xs text-slate-400">Confirm: {summary.confirmation_level?.condition || summary.confirmation_level?.price || '-'}</p>
          <p className="text-xs text-slate-400">Invalidate: {summary.invalidation_level?.condition || summary.invalidation_level?.price || '-'}</p>
        </div>

        <div className="rounded border border-slate-700 bg-slate-900/30 p-3">
          <p className="text-[11px] uppercase text-slate-400">Upcoming catalysts</p>
          {upcoming.length ? (
            <ul className="mt-1 space-y-1 text-slate-200">
              {upcoming.slice(0, 5).map((item, idx) => (
                <li key={`${item.symbol}-${idx}`} className="border-b border-slate-800 pb-1">
                  <span className="font-semibold">{item.symbol || '-'}</span> {item.name ? `- ${item.name}` : ''}
                  <div className="text-xs text-slate-400">{item.event_type || 'catalyst'} | {timeLabel(item.date_time)} | {' '}
                    {item.occurs_before_expiration ? 'before expiration' : 'after expiration'}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-slate-400">No upcoming scheduled catalysts found.</p>
          )}
        </div>
      </div>

      <div className="mt-3 rounded border border-slate-700 bg-slate-900/30 p-3">
        <p className="text-[11px] uppercase text-slate-400">Grouped events and sources</p>
        {events.length ? (
          <div className="mt-2 space-y-2">
            {events.slice(0, 8).map((event) => (
              <div key={`${event.headline}-${event.publication_timestamp}`} className="rounded border border-slate-800 bg-panel2 p-2">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="font-semibold text-slate-100">{event.headline || '-'}</p>
                    <p className="text-xs text-slate-400">{event.source || '-'} | {timeLabel(event.publication_timestamp)} | {event.event_category || '-'}</p>
                  </div>
                  <span className="badge border border-slate-700 bg-slate-900/30 text-xs">{event.position_impact || '-'}</span>
                </div>
                <p className="mt-1 text-xs text-slate-300">{event.news_reaction_classification || '-'}</p>
                <p className="mt-1 text-xs text-slate-400">
                  15m {pct(event.actual_share_price_reaction?.return_15m_pct)} | 1h {pct(event.actual_share_price_reaction?.return_1h_pct)} | RVOL {event.actual_share_price_reaction?.relative_volume === null || event.actual_share_price_reaction?.relative_volume === undefined ? '-' : `${Number(event.actual_share_price_reaction.relative_volume).toFixed(2)}x`}
                </p>
                {event.url ? <a className="mt-1 inline-block text-xs text-sky-300 underline" href={event.url} target="_blank" rel="noreferrer">Open source</a> : null}
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-1 text-slate-400">No grouped recent events found for this symbol.</p>
        )}
      </div>

      {sourceList.length ? (
        <div className="mt-3 text-xs text-slate-400">
          Sources: {sourceList.join(', ')}
        </div>
      ) : null}
    </details>
  )
}
