from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import desc

from ..config import config_manager
from ..db import SessionLocal
from ..models import ProviderErrorLog
from .base import ProviderError


_lock = threading.Lock()
_request_times: dict[str, deque[float]] = defaultdict(deque)
_last_request_at: dict[str, float] = {}
_backoff_until: dict[str, float] = {}
_backoff_attempts: dict[str, int] = defaultdict(int)
_last_error: dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _provider_cfg(provider_name: str) -> dict[str, Any]:
    rate_cfg = config_manager.get("rate_limits", default={}) or {}
    return rate_cfg.get(str(provider_name or "").lower(), {}) or {}


def _enabled() -> bool:
    rate_cfg = config_manager.get("rate_limits", default={}) or {}
    return bool(rate_cfg.get("enabled", True))


def _prune(provider: str, now: float) -> None:
    window = _request_times[provider]
    while window and now - window[0] > 3600:
        window.popleft()


def rate_limit(provider_name: str) -> dict[str, Any]:
    provider = str(provider_name or "").lower()
    if not provider or not _enabled():
        return {"provider": provider, "slept_seconds": 0.0}

    slept = 0.0
    while True:
        with _lock:
            now = time.time()
            _prune(provider, now)
            cfg = _provider_cfg(provider)
            min_gap = float(cfg.get("min_seconds_between_requests", 0) or 0)
            rpm = int(cfg.get("requests_per_minute", 0) or 0)
            rph = int(cfg.get("requests_per_hour", 0) or 0)
            waits: list[float] = []

            last = _last_request_at.get(provider)
            if last is not None and min_gap > 0:
                waits.append(max(0.0, min_gap - (now - last)))

            if rpm > 0:
                recent_minute = [ts for ts in _request_times[provider] if now - ts <= 60]
                if len(recent_minute) >= rpm:
                    waits.append(max(0.0, 60 - (now - recent_minute[0])))

            if rph > 0 and len(_request_times[provider]) >= rph:
                waits.append(max(0.0, 3600 - (now - _request_times[provider][0])))

            backoff_wait = max(0.0, _backoff_until.get(provider, 0.0) - now)
            if backoff_wait > 0:
                waits.append(backoff_wait)

            wait = max(waits) if waits else 0.0
            if wait <= 0:
                _last_request_at[provider] = now
                _request_times[provider].append(now)
                return {"provider": provider, "slept_seconds": round(slept, 3)}

        time.sleep(min(wait, 60.0))
        slept += min(wait, 60.0)


def detect_rate_limit_error(error: Any) -> bool:
    if isinstance(error, ProviderError) and getattr(error, "rate_limited", False):
        return True
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in [
            "too many requests",
            "rate limited",
            "rate-limit",
            "rate limit",
            "http 429",
            " 429",
            "quota exceeded",
        ]
    )


def provider_cooldown(provider_name: str, db=None, now_ts: float | None = None) -> dict[str, Any]:
    provider = str(provider_name or "").lower()
    cfg = _provider_cfg(provider)
    cooldown_seconds = int(cfg.get("cooldown_after_rate_limit_seconds", 0) or 0)
    if not provider or cooldown_seconds <= 0:
        return {"provider": provider, "active": False, "remaining_seconds": 0.0}

    close_db = db is None
    session = db or SessionLocal()
    now = time.time() if now_ts is None else now_ts
    try:
        last_error = (
            session.query(ProviderErrorLog)
            .filter(ProviderErrorLog.provider == provider)
            .filter(ProviderErrorLog.error_type == "rate_limit")
            .order_by(desc(ProviderErrorLog.created_at))
            .first()
        )
        if not last_error:
            return {"provider": provider, "active": False, "remaining_seconds": 0.0}

        created = _parse_iso(last_error.created_at)
        if not created:
            return {"provider": provider, "active": False, "remaining_seconds": 0.0}

        until = created.timestamp() + cooldown_seconds
        remaining = max(0.0, until - now)
        return {
            "provider": provider,
            "active": remaining > 0,
            "remaining_seconds": round(remaining, 2),
            "cooldown_seconds": cooldown_seconds,
            "cooldown_until": datetime.fromtimestamp(until, timezone.utc).isoformat(),
            "last_rate_limit_at": created.isoformat(),
            "last_error": last_error.error_message,
        }
    finally:
        if close_db:
            session.close()


def provider_backoff(provider_name: str, error: Any) -> dict[str, Any]:
    provider = str(provider_name or "").lower()
    cfg = _provider_cfg(provider)
    initial = float(cfg.get("backoff_initial_seconds", 10) or 10)
    maximum = float(cfg.get("backoff_max_seconds", 300) or 300)
    max_retries = int(cfg.get("max_retries", 0) or 0)

    with _lock:
        attempt = _backoff_attempts[provider] + 1
        if attempt > max_retries:
            _last_error[provider] = str(error)
            return {"provider": provider, "attempt": attempt, "stopped": True, "slept_seconds": 0.0}
        _backoff_attempts[provider] = attempt
        base = min(maximum, initial * (2 ** max(0, attempt - 1)))
        wait = min(maximum, base + random.uniform(0, min(base * 0.25, 5.0)))
        _backoff_until[provider] = time.time() + wait
        _last_error[provider] = str(error)

    time.sleep(wait)
    return {"provider": provider, "attempt": attempt, "slept_seconds": round(wait, 3)}


def record_provider_error(
    provider: str,
    symbol: str | None,
    endpoint: str | None,
    error: Any,
    retry_after_seconds: int | None = None,
) -> None:
    provider_name = str(provider or "unknown").lower()
    error_type = "rate_limit" if detect_rate_limit_error(error) else type(error).__name__
    message = str(error)
    with _lock:
        _last_error[provider_name] = message

    db = SessionLocal()
    try:
        db.add(
            ProviderErrorLog(
                provider=provider_name,
                symbol=(symbol or None),
                endpoint=(endpoint or None),
                error_message=message[:2000],
                error_type=error_type,
                retry_after_seconds=retry_after_seconds,
                created_at=_now_iso(),
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def reset_provider_backoff(provider_name: str) -> None:
    provider = str(provider_name or "").lower()
    with _lock:
        _backoff_attempts[provider] = 0
        _backoff_until[provider] = 0.0


def call_with_rate_limit(
    provider_name: str,
    symbol: str | None,
    endpoint: str,
    fn: Callable[..., Any],
    *args,
    **kwargs,
) -> Any:
    provider = str(provider_name or "").lower()
    cfg = _provider_cfg(provider)
    max_retries = int(cfg.get("max_retries", 0) or 0)
    attempt = 0

    while True:
        cooldown = provider_cooldown(provider)
        if cooldown.get("active"):
            remaining = int(float(cooldown.get("remaining_seconds") or 0))
            raise ProviderError(
                f"{provider} provider cooldown active for {remaining} seconds after recent rate limit",
                rate_limited=True,
                provider=provider,
            )
        rate_limit(provider)
        try:
            result = fn(*args, **kwargs)
            reset_provider_backoff(provider)
            return result
        except Exception as exc:
            record_provider_error(provider, symbol, endpoint, exc)
            if detect_rate_limit_error(exc) and attempt < max_retries:
                attempt += 1
                provider_backoff(provider, exc)
                continue
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(str(exc), rate_limited=detect_rate_limit_error(exc), provider=provider) from exc


def provider_status() -> list[dict[str, Any]]:
    rate_cfg = config_manager.get("rate_limits", default={}) or {}
    providers = [name for name in rate_cfg.keys() if name != "enabled"]
    rows: list[dict[str, Any]] = []
    now = time.time()

    db = SessionLocal()
    try:
        for provider in providers:
            provider_name = str(provider).lower()
            cfg = _provider_cfg(provider_name)
            recent_error_count = (
                db.query(ProviderErrorLog)
                .filter(ProviderErrorLog.provider == provider_name)
                .filter(ProviderErrorLog.created_at >= datetime.fromtimestamp(now - 3600, timezone.utc).isoformat())
                .count()
            )
            last_error = (
                db.query(ProviderErrorLog)
                .filter(ProviderErrorLog.provider == provider_name)
                .order_by(desc(ProviderErrorLog.created_at))
                .first()
            )
            with _lock:
                last_request = _last_request_at.get(provider_name)
                backoff_until = _backoff_until.get(provider_name, 0.0)
                backoff_remaining = max(0.0, backoff_until - now)
                attempts = _backoff_attempts.get(provider_name, 0)
            cooldown = provider_cooldown(provider_name, db=db, now_ts=now)
            cooldown_remaining = float(cooldown.get("remaining_seconds") or 0.0)
            blocked_remaining = max(backoff_remaining, cooldown_remaining)
            blocked_source = None
            if cooldown_remaining >= backoff_remaining and cooldown_remaining > 0:
                blocked_source = "persistent_cooldown"
            elif backoff_remaining > 0:
                blocked_source = "in_memory_backoff"
            rows.append(
                {
                    "provider": provider_name,
                    "requests_per_minute": cfg.get("requests_per_minute"),
                    "requests_per_hour": cfg.get("requests_per_hour"),
                    "min_seconds_between_requests": cfg.get("min_seconds_between_requests"),
                    "last_request_time": (
                        datetime.fromtimestamp(last_request, timezone.utc).isoformat()
                        if last_request
                        else None
                    ),
                    "current_backoff_state": {
                        "active": blocked_remaining > 0,
                        "attempts": attempts,
                        "remaining_seconds": round(blocked_remaining, 2),
                        "source": blocked_source,
                        "cooldown_until": cooldown.get("cooldown_until"),
                    },
                    "recent_error_count": recent_error_count,
                    "available": blocked_remaining <= 0,
                    "last_error": last_error.error_message if last_error else None,
                }
            )
    finally:
        db.close()
    return rows
