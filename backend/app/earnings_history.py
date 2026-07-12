from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import config_manager
from .history import get_candles_from_sql
from .providers import provider_factory
from .providers.rate_limiter import detect_rate_limit_error


EASTERN = ZoneInfo("America/New_York")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "None", "null", "N/A", "na"):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _cache_path(symbol: str) -> Path:
    cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"earnings_history_{symbol.upper()}.json"


def _read_cache(path: Path, ttl: int, force_refresh: bool) -> list[dict[str, Any]] | None:
    if force_refresh or not path.exists():
        return None
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        payload = json.loads(path.read_text())
        rows = payload.get("events") if isinstance(payload, dict) else None
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def _write_cache(path: Path, symbol: str, provider: str, rows: list[dict[str, Any]]) -> None:
    try:
        path.write_text(
            json.dumps(
                {
                    "symbol": symbol,
                    "provider": provider,
                    "retrieved_at": _now_iso(),
                    "events": rows,
                },
                sort_keys=True,
            )
        )
    except Exception:
        pass


def _numeric_result(actual: Any, estimate: Any) -> str:
    actual_number = _safe_float(actual)
    estimate_number = _safe_float(estimate)
    if actual_number is None or estimate_number is None:
        return "UNKNOWN"
    if actual_number > estimate_number:
        return "BEAT"
    if actual_number < estimate_number:
        return "MISS"
    return "IN_LINE"


def _overall_result(eps_result: str, revenue_result: str) -> str:
    known = [value for value in (eps_result, revenue_result) if value != "UNKNOWN"]
    if not known:
        return "UNKNOWN"
    if all(value == "BEAT" for value in known):
        return "BEAT"
    if all(value == "MISS" for value in known):
        return "MISS"
    if any(value == "BEAT" for value in known) and any(value == "MISS" for value in known):
        return "MIXED"
    return "IN_LINE"


def _report_timing(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "after" in text or "close" in text or "post" in text:
        return "AFTER_CLOSE"
    if "before" in text or "open" in text or "pre-market" in text or "premarket" in text:
        return "BEFORE_OPEN"
    return "UNKNOWN"


def _daily_rows(symbol: str, db) -> list[dict[str, Any]]:
    frame = get_candles_from_sql(symbol, "1d", period="max", db=db)
    if frame is None or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for timestamp, row in frame.iterrows():
        try:
            parsed = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed_et = parsed.astimezone(EASTERN)
            rows.append(
                {
                    "date": parsed_et.date(),
                    "timestamp": parsed.astimezone(timezone.utc).isoformat(),
                    "open": _safe_float(row.get("open")),
                    "high": _safe_float(row.get("high")),
                    "low": _safe_float(row.get("low")),
                    "close": _safe_float(row.get("close")),
                    "volume": _safe_float(row.get("volume")),
                }
            )
        except Exception:
            continue
    return sorted(rows, key=lambda item: item["date"])


def _reaction(event: dict[str, Any], daily: list[dict[str, Any]]) -> dict[str, Any]:
    report_date = _parse_date(event.get("reported_date"))
    if report_date is None or not daily:
        return {
            "data_status": "unavailable",
            "confidence": "LOW",
            "reaction_note": "Reported date or stored daily candles are unavailable.",
        }

    timing = _report_timing(event.get("report_time"))
    prior_candidates = [idx for idx, row in enumerate(daily) if row["date"] < report_date]
    if timing == "AFTER_CLOSE":
        reaction_candidates = [idx for idx, row in enumerate(daily) if row["date"] > report_date]
        prior_candidates = [idx for idx, row in enumerate(daily) if row["date"] <= report_date]
    elif timing == "BEFORE_OPEN":
        reaction_candidates = [idx for idx, row in enumerate(daily) if row["date"] >= report_date]
    else:
        reaction_candidates = [idx for idx, row in enumerate(daily) if row["date"] >= report_date]

    if not prior_candidates or not reaction_candidates:
        return {
            "data_status": "unavailable",
            "confidence": "LOW",
            "reaction_note": "Stored daily candles do not cover the earnings reaction window.",
        }

    prior = daily[prior_candidates[-1]]
    reaction_index = reaction_candidates[0]
    baseline = prior.get("close")
    first = daily[reaction_index]
    if baseline is None or baseline == 0:
        return {
            "data_status": "unavailable",
            "confidence": "LOW",
            "reaction_note": "Prior close is unavailable.",
        }

    def change(value: float | None) -> float | None:
        return round((value / baseline - 1.0) * 100.0, 3) if value is not None else None

    forward: dict[str, float | None] = {}
    for offset in (1, 3, 5):
        target_index = reaction_index + offset - 1
        forward[f"{offset}_session_return_pct"] = change(daily[target_index].get("close")) if target_index < len(daily) else None

    window = daily[reaction_index : min(len(daily), reaction_index + 5)]
    highs = [row["high"] for row in window if row.get("high") is not None]
    lows = [row["low"] for row in window if row.get("low") is not None]
    return {
        "data_status": "observed",
        "confidence": "HIGH" if len(window) >= 5 else "MODERATE",
        "prior_close": round(baseline, 4),
        "reaction_session": first["date"].isoformat(),
        "reaction_timestamp": first.get("timestamp"),
        "first_open": first.get("open"),
        "first_close": first.get("close"),
        "gap_pct": change(first.get("open")),
        "first_session_return_pct": change(first.get("close")),
        "max_favorable_move_pct": round((max(highs) / baseline - 1.0) * 100.0, 3) if highs else None,
        "max_adverse_move_pct": round((min(lows) / baseline - 1.0) * 100.0, 3) if lows else None,
        "volume": first.get("volume"),
        **forward,
        "reaction_note": "Reaction measured from the last stored close before the reported earnings event.",
    }


def _normalize_event(row: dict[str, Any]) -> dict[str, Any]:
    eps_result = _numeric_result(row.get("reported_eps"), row.get("estimated_eps"))
    revenue_result = _numeric_result(row.get("reported_revenue"), row.get("estimated_revenue"))
    return {
        "fiscal_date_ending": row.get("fiscal_date_ending"),
        "reported_date": row.get("reported_date"),
        "report_time": row.get("report_time"),
        "report_timing": _report_timing(row.get("report_time")),
        "reported_eps": _safe_float(row.get("reported_eps")),
        "estimated_eps": _safe_float(row.get("estimated_eps")),
        "eps_result": eps_result,
        "reported_revenue": _safe_float(row.get("reported_revenue")),
        "estimated_revenue": _safe_float(row.get("estimated_revenue")),
        "revenue_result": revenue_result,
        "surprise": _safe_float(row.get("surprise")),
        "surprise_percentage": _safe_float(row.get("surprise_percentage")),
        "overall_result": _overall_result(eps_result, revenue_result),
        "provider": row.get("provider") or "unknown",
        "data_status": "observed",
    }


def build_earnings_profile(symbol: str, db, *, force_refresh: bool = False) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    cfg = config_manager.get("earnings_history", default={}) or {}
    if not bool(cfg.get("enabled", True)):
        return {"symbol": normalized, "data_status": "disabled", "events": [], "last_earnings": None}

    ttl = int(cfg.get("cache_ttl_seconds", 86400) or 86400)
    cache_path = _cache_path(normalized)
    raw = _read_cache(cache_path, ttl, force_refresh)
    provider_name = str(cfg.get("provider") or "alphavantage").strip().lower()
    warning = None
    if raw is None:
        selection = provider_factory.get_selection("earnings_provider", provider_name)
        try:
            raw, provider_name, warning = provider_factory.with_fallback(
                selection,
                "get_earnings_history",
                normalized,
                force_refresh=force_refresh,
            )
            if not isinstance(raw, list):
                raise ValueError("Earnings provider returned malformed data")
            _write_cache(cache_path, normalized, provider_name, raw)
        except Exception as exc:
            warning = str(exc)
            raw = []
            if detect_rate_limit_error(exc):
                warning = f"Earnings provider rate-limited: {warning}"

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=int(cfg.get("lookback_days", 365) or 365))
    events: list[dict[str, Any]] = []
    daily = _daily_rows(normalized, db)
    for raw_event in raw:
        event = _normalize_event(raw_event)
        reported_date = _parse_date(event.get("reported_date"))
        if reported_date and reported_date < cutoff:
            continue
        event["price_reaction"] = _reaction(event, daily)
        events.append(event)

    events.sort(key=lambda item: str(item.get("reported_date") or ""), reverse=True)
    last = events[0] if events else None
    return {
        "symbol": normalized,
        "lookback_days": int(cfg.get("lookback_days", 365) or 365),
        "provider": provider_name if events or raw else None,
        "retrieved_at": _now_iso() if raw else None,
        "event_count": len(events),
        "events": events,
        "last_earnings": last,
        "data_status": "observed" if events else "unavailable",
        "warning": warning,
        "note": "Beat/miss is based on reported versus estimated EPS and revenue when both are supplied. Price reaction uses stored daily candles and is unavailable when coverage is missing.",
    }
