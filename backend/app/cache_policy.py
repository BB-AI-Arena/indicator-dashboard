from __future__ import annotations

from typing import Any

from .config import config_manager


def _closed_session_ttl(default: int = 18000) -> int:
    try:
        return int(config_manager.get("cache", "market_closed_ttl_seconds", default=default) or default)
    except Exception:
        return default


def market_aware_ttl(base_ttl_seconds: int | float | None, *, market_session: dict[str, Any] | None = None) -> int:
    """Return the normal TTL during live sessions and a longer floor while planning off-hours."""
    try:
        base_ttl = max(0, int(base_ttl_seconds or 0))
    except Exception:
        base_ttl = 0

    try:
        session = market_session
        if session is None:
            from .market_session import get_market_session

            session = get_market_session()
        if session and not bool(session.get("actionable_live_quotes")):
            return max(base_ttl, _closed_session_ttl())
    except Exception:
        return base_ttl
    return base_ttl
