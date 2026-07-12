from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .config import config_manager
from .data_provider import fetch_candles
from .db import SessionLocal
from .providers.base import ProviderError
from .scanner import ensure_watchlist_seeded, get_watchlist_symbols


_candle_refresh_task: asyncio.Task | None = None
_candle_worker_status: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_tick_at": None,
    "current_symbol": None,
    "ok_count": 0,
    "error_count": 0,
    "last_error": None,
}


def _worker_cfg() -> dict[str, Any]:
    return (config_manager.config or {}).get("yahoo_worker", {})


def status() -> dict[str, Any]:
    return dict(_candle_worker_status)


async def _run_loop() -> None:
    global _candle_worker_status
    cfg = config_manager.config or {}
    next_retry: dict[str, float] = {}

    _candle_worker_status["running"] = True
    _candle_worker_status["started_at"] = datetime.now(timezone.utc).isoformat()

    while True:
        config_manager.reload()
        scan_cfg = config_manager.get("scan", default={}) or {}
        wcfg = _worker_cfg()
        interval = str(scan_cfg.get("interval", "5m"))
        period = str(scan_cfg.get("period", "5d"))
        symbol_delay = float(wcfg.get("symbol_delay_seconds", 2.0))
        idle_delay = float(wcfg.get("idle_delay_seconds", 10.0))
        max_backoff = float(wcfg.get("max_backoff_seconds", 300.0))

        db = SessionLocal()
        try:
            ensure_watchlist_seeded(db, list(scan_cfg.get("symbols", [])))
            symbols = get_watchlist_symbols(db)
        finally:
            db.close()

        now = asyncio.get_event_loop().time()
        eligible = [s for s in symbols if next_retry.get(s, 0.0) <= now]
        if not eligible:
            await asyncio.sleep(max(idle_delay, 1.0))
            continue

        for symbol in eligible:
            _candle_worker_status["current_symbol"] = symbol
            _candle_worker_status["last_tick_at"] = datetime.now(timezone.utc).isoformat()
            try:
                await asyncio.to_thread(fetch_candles, symbol, interval=interval, period=period)
                next_retry[symbol] = 0.0
                _candle_worker_status["ok_count"] = int(_candle_worker_status.get("ok_count", 0)) + 1
                _candle_worker_status["last_error"] = None
            except ProviderError as exc:
                _candle_worker_status["error_count"] = int(_candle_worker_status.get("error_count", 0)) + 1
                _candle_worker_status["last_error"] = str(exc)
                prior = next_retry.get(symbol, now)
                # Exponential-ish backoff per symbol with cap.
                wait = min(max_backoff, max(symbol_delay * 2.0, (prior - now) * 2.0 if prior > now else symbol_delay * 2.0))
                if getattr(exc, "rate_limited", False):
                    wait = min(max_backoff, max(wait, symbol_delay * 5.0))
                next_retry[symbol] = now + max(wait, symbol_delay)
            except Exception as exc:
                _candle_worker_status["error_count"] = int(_candle_worker_status.get("error_count", 0)) + 1
                _candle_worker_status["last_error"] = str(exc)
                next_retry[symbol] = now + max(symbol_delay * 3.0, 5.0)

            await asyncio.sleep(max(symbol_delay, 0.2))


def start_if_enabled() -> None:
    global _candle_refresh_task
    data_cfg = config_manager.get("data", default={}) or {}
    if str(data_cfg.get("candles_provider", "")).strip().lower() != "yahoo":
        return
    if not bool(_worker_cfg().get("enabled", True)):
        return
    if _candle_refresh_task is None or _candle_refresh_task.done():
        _candle_refresh_task = asyncio.create_task(_run_loop())


async def stop() -> None:
    global _candle_refresh_task
    if _candle_refresh_task:
        _candle_refresh_task.cancel()
        try:
            await _candle_refresh_task
        except asyncio.CancelledError:
            pass
        _candle_refresh_task = None
    _candle_worker_status["running"] = False
