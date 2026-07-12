import { formatEasternTime } from '../utils/time'

export default function MarketStatusBanner({ marketSession }) {
  const state = String(marketSession?.session_state || '').toUpperCase()
  const actionable = Boolean(marketSession?.actionable_live_quotes)
  const tone = actionable ? 'market-open' : 'market-closed'

  const headline = actionable ? 'MARKET OPEN' : 'MARKET CLOSED'
  const stateLabel = state || 'LOADING'
  const currentTime = marketSession?.current_eastern_timestamp ? formatEasternTime(marketSession.current_eastern_timestamp) : '-'
  const nextOpen = marketSession?.next_market_open ? formatEasternTime(marketSession.next_market_open, { seconds: false }) : '-'
  const regularOpen = marketSession?.regular_session_open ? formatEasternTime(marketSession.regular_session_open, { seconds: false }) : '-'
  const regularClose = marketSession?.regular_session_close ? formatEasternTime(marketSession.regular_session_close, { seconds: false }) : '-'

  return (
    <div className={`market-session-banner ${tone}`}>
      <div className="market-headline">
        <span className="market-pulse" aria-hidden="true" />
        <div>
          <div className="market-title">{headline}</div>
          <div className="market-state">{stateLabel}</div>
        </div>
      </div>
      <div className="market-note">
        {marketSession?.session_note || 'Loading market session...'}
      </div>
      <div className="market-session-grid">
        <div>
          <span>ET Now</span>
          <strong>{currentTime}</strong>
        </div>
        <div>
          <span>Regular Open</span>
          <strong>{regularOpen}</strong>
        </div>
        <div>
          <span>Regular Close</span>
          <strong>{regularClose}</strong>
        </div>
        <div>
          <span>Next Open</span>
          <strong>{nextOpen}</strong>
        </div>
        {marketSession?.minutes_until_open != null ? (
          <div>
            <span>Until Open</span>
            <strong>{marketSession.minutes_until_open} min</strong>
          </div>
        ) : null}
        {marketSession?.minutes_until_close != null ? (
          <div>
            <span>Until Close</span>
            <strong>{marketSession.minutes_until_close} min</strong>
          </div>
        ) : null}
      </div>
    </div>
  )
}
