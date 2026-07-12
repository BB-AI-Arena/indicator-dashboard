from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


EASTERN = ZoneInfo("America/New_York")
PREMARKET_START = dtime(4, 0)
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
AFTER_HOURS_END = dtime(20, 0)


def _to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN)


def _iso_et(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_et(value).isoformat()
    try:
        return _to_et(datetime.fromisoformat(str(value).replace("Z", "+00:00"))).isoformat()
    except Exception:
        return None


def _ceil_minutes(delta_seconds: float) -> int:
    if delta_seconds <= 0:
        return 0
    return int((delta_seconds + 59) // 60)


@lru_cache(maxsize=1)
def _calendar():
    return xcals.get_calendar("XNYS")


def _is_weekend(now_et: datetime) -> bool:
    return now_et.weekday() >= 5


def _is_us_market_holiday(now_et: datetime) -> bool:
    if _is_weekend(now_et):
        return False
    cal = _calendar()
    return not cal.is_session(now_et.date())


def _session_reference_date(now_utc: datetime) -> datetime.date:
    cal = _calendar()
    now_et = _to_et(now_utc)
    if cal.is_session(now_et.date()):
        return now_et.date()
    next_open = cal.next_open(now_utc)
    return _to_et(next_open.to_pydatetime()).date()


def _session_open_close(reference_date) -> tuple[datetime, datetime]:
    cal = _calendar()
    open_utc = cal.session_open(reference_date).to_pydatetime()
    close_utc = cal.session_close(reference_date).to_pydatetime()
    return _to_et(open_utc), _to_et(close_utc)


def _session_state(now_et: datetime, regular_open: datetime, regular_close: datetime) -> str:
    minute = now_et.hour * 60 + now_et.minute
    open_minute = regular_open.hour * 60 + regular_open.minute
    close_minute = regular_close.hour * 60 + regular_close.minute

    if _is_weekend(now_et):
        return "MARKET_CLOSED"
    if _is_us_market_holiday(now_et):
        return "HOLIDAY"
    if minute < PREMARKET_START.hour * 60 + PREMARKET_START.minute:
        return "MARKET_CLOSED"
    if minute < open_minute:
        return "PREMARKET"
    if minute < close_minute:
        return "EARLY_CLOSE" if close_minute < REGULAR_CLOSE.hour * 60 + REGULAR_CLOSE.minute else "REGULAR"
    if minute < AFTER_HOURS_END.hour * 60 + AFTER_HOURS_END.minute:
        return "AFTER_HOURS"
    return "MARKET_CLOSED"


def _session_note(state: str, next_open: datetime | None) -> str:
    if state == "PREMARKET":
        return "Market closed - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open. Refresh option chain after 9:30 AM ET."
    if state == "AFTER_HOURS":
        return "Market closed - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open."
    if state == "HOLIDAY":
        return "US market holiday - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open."
    if state == "MARKET_CLOSED":
        next_text = _iso_et(next_open) if next_open else None
        suffix = f" Next market open: {next_text}." if next_text else ""
        return "Market closed - planning for the next session. Option quote, spread, volume, and Greeks are from the most recent available session and must be refreshed after the next open." + suffix
    if state == "EARLY_CLOSE":
        return "Early-close session - live quotes are actionable until the shortened close."
    return "Regular session - live option quotes are actionable."


def get_market_session(now: datetime | None = None) -> dict[str, Any]:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    now_et = _to_et(now_utc)

    cal = _calendar()
    reference_date = _session_reference_date(now_utc)
    regular_open_et, regular_close_et = _session_open_close(reference_date)
    next_open_utc = cal.next_open(now_utc).to_pydatetime()
    next_open_et = _to_et(next_open_utc)
    state = _session_state(now_et, regular_open_et, regular_close_et)
    actionable_live_quotes = state in {"REGULAR", "EARLY_CLOSE"}

    if actionable_live_quotes:
        minutes_until_open = 0
        minutes_until_close = _ceil_minutes((regular_close_et - now_et).total_seconds())
    else:
        minutes_until_open = _ceil_minutes((next_open_et - now_et).total_seconds())
        minutes_until_close = 0

    is_early_close_day = regular_close_et.time() < REGULAR_CLOSE
    session_state = state

    return {
        "timezone": "America/New_York",
        "current_eastern_timestamp": now_et.isoformat(),
        "session_state": session_state,
        "market_open": actionable_live_quotes,
        "actionable_live_quotes": actionable_live_quotes,
        "reference_session_date": reference_date.isoformat(),
        "regular_session_open": regular_open_et.isoformat(),
        "regular_session_close": regular_close_et.isoformat(),
        "next_market_open": next_open_et.isoformat(),
        "minutes_until_open": minutes_until_open,
        "minutes_until_close": minutes_until_close,
        "option_quote_session_label": "Live" if actionable_live_quotes else "Previous session",
        "underlying_session_label": "Live" if actionable_live_quotes or state == "PREMARKET" else "Previous session",
        "planning_mode": not actionable_live_quotes,
        "is_early_close_day": bool(regular_close_et.time() < REGULAR_CLOSE),
        "session_note": _session_note(session_state, next_open_et),
    }
