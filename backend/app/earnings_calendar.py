from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .cache_policy import market_aware_ttl
from .config import config_manager
from .db import SessionLocal
from .models import Watchlist


EASTERN = ZoneInfo("America/New_York")
_lock = threading.Lock()
_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _watchlist_symbols() -> set[str]:
    db = SessionLocal()
    try:
        return {str(row.symbol or "").upper() for row in db.query(Watchlist).filter(Watchlist.active.is_(True)).all()}
    finally:
        db.close()


def _week_window(now_et: datetime) -> tuple[date, date]:
    today = now_et.date()
    if today.weekday() >= 5:
        start = today + timedelta(days=(7 - today.weekday()))
    else:
        start = today
    end = start + timedelta(days=(4 - start.weekday()))
    return start, end


def _date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _time_label(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if "pre" in text or "before" in text:
        return "Before Open"
    if "after" in text or "post" in text:
        return "After Close"
    return "Time TBA"


def _event_datetime(day: date, raw_time: str | None) -> datetime:
    label = _time_label(raw_time)
    if label == "Before Open":
        event_time = dtime(9, 30)
    elif label == "After Close":
        event_time = dtime(16, 0)
    else:
        event_time = dtime(16, 0)
    return datetime.combine(day, event_time, tzinfo=EASTERN)


def _market_cap_number(value: str | None) -> float:
    text = str(value or "").upper().replace("$", "").replace(",", "").strip()
    if not text or text == "N/A":
        return 0.0
    multiplier = 1.0
    if text.endswith("T"):
        multiplier = 1_000_000_000_000.0
        text = text[:-1]
    elif text.endswith("B"):
        multiplier = 1_000_000_000.0
        text = text[:-1]
    elif text.endswith("M"):
        multiplier = 1_000_000.0
        text = text[:-1]
    try:
        return float(text) * multiplier
    except Exception:
        digits = re.sub(r"[^0-9.]", "", text)
        try:
            return float(digits) if digits else 0.0
        except Exception:
            return 0.0


def _fetch_day(day: date, timeout: int) -> tuple[list[dict[str, Any]], str | None]:
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
    try:
        response = requests.get(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Mozilla/5.0 indicator-dashboard earnings calendar",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/market-activity/earnings",
            },
            timeout=timeout,
        )
        if response.status_code >= 400:
            return [], f"Nasdaq earnings {day.isoformat()}: HTTP {response.status_code}"
        payload = response.json()
        rows = ((payload.get("data") or {}).get("rows") or [])
        if not isinstance(rows, list):
            return [], f"Nasdaq earnings {day.isoformat()}: malformed rows"
        return rows, None
    except Exception as exc:
        return [], f"Nasdaq earnings {day.isoformat()}: {exc}"


def _normalize_row(row: dict[str, Any], day: date, watchlist: set[str]) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper().strip()
    raw_time = str(row.get("time") or "")
    event_dt = _event_datetime(day, raw_time)
    return {
        "symbol": symbol,
        "symbols": [symbol] if symbol else [],
        "name": str(row.get("name") or "").strip(),
        "date": day.isoformat(),
        "time": _time_label(raw_time),
        "event_time_et": event_dt.isoformat(),
        "eps_forecast": str(row.get("epsForecast") or "").strip(),
        "last_year_eps": str(row.get("lastYearEPS") or "").strip(),
        "fiscal_quarter": str(row.get("fiscalQuarterEnding") or "").strip(),
        "market_cap": str(row.get("marketCap") or "").strip(),
        "market_cap_value": _market_cap_number(str(row.get("marketCap") or "")),
        "watchlist": symbol in watchlist,
        "source": "Nasdaq Earnings",
    }


def upcoming_earnings_feed(*, force_refresh: bool = False) -> dict[str, Any]:
    cfg = config_manager.get("earnings", default={}) or {}
    enabled = bool(cfg.get("enabled", True))
    ttl = market_aware_ttl(int(cfg.get("cache_ttl_seconds", 1800) or 1800))
    timeout = int(cfg.get("request_timeout_seconds", 8) or 8)
    max_items = int(cfg.get("max_items", 40) or 40)
    watchlist_only = bool(cfg.get("watchlist_only", False))

    if not enabled:
        return {"enabled": False, "items": [], "errors": [], "updated_at": _now_iso()}

    now = time.time()
    with _lock:
        cached = _cache.get("payload")
        if cached and not force_refresh and now < float(_cache.get("expires_at") or 0):
            return {**cached, "cached": True}

    now_et = datetime.now(EASTERN)
    start, end = _week_window(now_et)
    watchlist = _watchlist_symbols()
    errors: list[str] = []
    rows: list[dict[str, Any]] = []

    for day in _date_range(start, end):
        daily_rows, error = _fetch_day(day, timeout)
        if error:
            errors.append(error)
            continue
        for raw in daily_rows:
            item = _normalize_row(raw, day, watchlist)
            if not item["symbol"]:
                continue
            if watchlist_only and not item["watchlist"]:
                continue
            if _event_datetime(day, str(raw.get("time") or "")) <= now_et:
                continue
            rows.append(item)

    rows.sort(
        key=lambda item: (
            item.get("event_time_et") or "",
            0 if item.get("watchlist") else 1,
            -float(item.get("market_cap_value") or 0.0),
            item.get("symbol") or "",
        )
    )

    payload = {
        "enabled": True,
        "items": rows[:max_items],
        "errors": errors[:8],
        "source": "Nasdaq Earnings Calendar",
        "updated_at": _now_iso(),
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "cache_ttl_seconds": ttl,
        "cached": False,
    }
    with _lock:
        _cache["payload"] = payload
        _cache["expires_at"] = now + max(ttl, 300)
    return payload
