from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .advisory import build_advisory_package, generate_advisory, get_advisory_settings, update_advisory_settings
from .active_signals import ensure_active_signal_schema, get_active_signals, get_signal_history, record_signal_outcome, reconcile_signals, signal_worker_status, start_if_enabled as start_active_signal_worker, stop as stop_active_signal_worker, trigger_signal
from .ai_validator import validate_trade_gate
from .auth import etrade_auth
from .backtest import backtest_setup
from .candle_worker import start_if_enabled as start_candle_worker, status as candle_worker_status, stop as stop_candle_worker
from .cache_policy import market_aware_ttl
from .config import config_manager
from .data_provider import DataProviderError, fetch_candles, fetch_quote, normalize_symbol
from .decision_dashboard import build_decision_dashboard, build_watchlist_intelligence, ensure_core_universe
from .db import Base, SessionLocal, engine, get_db
from .earnings_calendar import upcoming_earnings_feed
from .etrade_positions import get_open_option_positions
from .market_session import get_market_session
from .morning_routine import build_morning_brief
from .news_catalyst import build_news_catalyst_impact
from .history import (
    create_or_resume_backfill_run,
    db_status as history_db_status,
    interval_seconds,
    latest_provider_error,
    now_iso,
    range_coverage_complete,
    stored_range,
    upsert_candles,
)
from .historical_patterns import build_historical_setup_match
from .indicators import apply_indicators
from .models import Alert, BackfillChunk, BackfillRun, BrokerageFill, BrokerageOrder, BrokerageTransaction, PaperMigrationReview, PaperMorningCandidate, Scan, TickerProfile, TradeReviewAccount, TradeReviewSyncRun, TradeReviewTrade, Watchlist
from .news_feeds import market_news_feed
from .options import calculate_ratios, get_option_expirations, ranked_contracts
from .option_estimation import latest_estimates, start_if_enabled as start_option_estimation_worker, status as option_estimation_status, stop as stop_option_estimation_worker
from .providers import provider_factory
from .providers.base import ProviderError
from .providers.rate_limiter import detect_rate_limit_error, provider_cooldown, provider_status
from .recommendation_performance import (
    get_recommendation_performance,
    list_recommendations,
    migrate_legacy_recommendations,
    resolve_recommendation,
    trigger_recommendation,
)
from .paper_portfolio import create_paper_order, ensure_paper_schema, get_paper_portfolio, migrate_ambiguous_legacy_records
from .profile_completeness import ensure_profile_schema, evaluate_profile_completeness
from .site_auth import site_auth
from .social_intelligence import build_social_profile, ensure_social_schema, get_social_settings, update_social_settings
from .ticker_profiles import ensure_ticker_profile, refresh_ticker_profile, serialize_ticker_profile
from .trade_review import (
    build_overview as build_trade_review_overview,
    build_trade_detail as build_trade_review_detail,
    cancel_active_sync as cancel_trade_review_sync,
    ensure_trade_review_schema,
    get_active_sync_run as get_trade_review_active_run,
    get_selection as get_trade_review_selection,
    reviewable_accounts,
    refresh_accounts as refresh_trade_review_accounts,
    list_trade_filters as list_trade_review_filters,
    pause_stale_sync_runs as pause_trade_review_stale_runs,
    run_sync as run_trade_review_sync,
    serialize_sync_run as serialize_trade_review_sync_run,
    _resolve_accounts_for_user,
    set_selection as set_trade_review_selection,
)
from .scanner import analyze_symbol, ensure_watchlist_seeded, get_watchlist_symbols, run_scan_for_symbols
from .schemas import HealthResponse, WatchlistAddRequest


Base.metadata.create_all(bind=engine)
ensure_active_signal_schema()
ensure_social_schema()
ensure_paper_schema()
ensure_profile_schema()
with SessionLocal() as _bootstrap_db:
    site_auth.ensure_schema(_bootstrap_db)
    ensure_trade_review_schema(_bootstrap_db)
    site_auth.seed_default_users(_bootstrap_db)
    migrate_legacy_recommendations(_bootstrap_db)
    migrate_ambiguous_legacy_records(_bootstrap_db)
    for _profile in _bootstrap_db.query(TickerProfile).all():
        evaluate_profile_completeness(_bootstrap_db, _profile, persist=True)
    _bootstrap_db.commit()

app = FastAPI(title="Indicator Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/bootstrap",
    "/api/market/session",
}

PASSWORD_CHANGE_ALLOWED_PATHS = {
    "/api/auth/status",
    "/api/auth/me",
    "/api/auth/logout",
    "/api/auth/change-password",
}

_latest_scan_results: dict[str, dict[str, Any]] = {}
_last_scan_time: str | None = None
_scanner_task: asyncio.Task | None = None
_scan_refresh_task: asyncio.Task | None = None
_backfill_task: asyncio.Task | None = None
_trade_review_task: asyncio.Task | None = None
_earnings_profile_task: asyncio.Task | None = None


@app.middleware("http")
async def auth_gate_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)

    db = SessionLocal()
    try:
        ip = site_auth.client_ip(request)
        blocked = site_auth.blocked_ip(db, ip)
        if blocked:
            return JSONResponse(status_code=403, content={"detail": f"Access blocked for this IP: {blocked['reason']}"})
        site_auth.enforce_geo(request)
        if path in PUBLIC_API_PATHS:
            return await call_next(request)

        current = site_auth.current_auth(db, request)
        if not current:
            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        if getattr(current.user, "must_change_password", False) and path not in PASSWORD_CHANGE_ALLOWED_PATHS:
            return JSONResponse(status_code=403, content={"detail": "Password change required"})
        request.state.current_auth = current
        return await call_next(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    finally:
        db.close()


def _scan_cache_stale() -> bool:
    if not _last_scan_time:
        return True
    try:
        last = datetime.fromisoformat(_last_scan_time.replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    ttl = market_aware_ttl(int(config_manager.get("cache", "scan_ttl_seconds", default=60)))
    return (datetime.now(timezone.utc) - last).total_seconds() > max(ttl, 5)


def _latest_scans_from_db(db: Session) -> dict[str, dict[str, Any]]:
    rows = db.query(Scan).order_by(desc(Scan.created_at)).limit(5000).all()
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.symbol in latest:
            continue
        latest[r.symbol] = {
            "symbol": r.symbol,
            "price": float(r.price or 0.0),
            "side": r.side or "NEUTRAL",
            "score": int(r.score or 0),
            "max_score": int(r.max_score or 8),
            "grade": r.grade or "NO_TRADE",
            "reasons": (r.reasons or "").split(" | ") if r.reasons else [],
            "warnings": (r.warnings or "").split(" | ") if r.warnings else [],
            "timestamp": r.created_at,
            "indicators": {},
            "option_ratios": None,
            "alert": False,
        }
    return latest


def _compute_scan_snapshot() -> tuple[dict[str, dict[str, Any]], str]:
    from .db import SessionLocal

    db = SessionLocal()
    try:
        cfg = config_manager.reload()
        scan_cfg = cfg.get("scan", {})
        indicator_cfg = cfg.get("indicators", {})
        # Bulk dashboard scan: skip per-symbol options chains to avoid long-running requests/timeouts.
        options_cfg = {"enabled": False}
        symbols = get_watchlist_symbols(db)
        results = run_scan_for_symbols(db, symbols, scan_cfg, indicator_cfg, options_cfg)
        return {r["symbol"]: r for r in results}, datetime.now(timezone.utc).isoformat()
    finally:
        db.close()


async def _run_scan_refresh():
    global _latest_scan_results, _last_scan_time, _scan_refresh_task
    try:
        results, ts = await asyncio.to_thread(_compute_scan_snapshot)
        _latest_scan_results = results
        _last_scan_time = ts
    finally:
        _scan_refresh_task = None


def _start_scan_refresh_if_needed():
    global _scan_refresh_task
    if _scan_refresh_task is None or _scan_refresh_task.done():
        _scan_refresh_task = asyncio.create_task(_run_scan_refresh())


def _request_auth(request: Request):
    current = getattr(request.state, "current_auth", None)
    if not current:
        raise HTTPException(status_code=401, detail="Authentication required")
    return current


def _request_admin(request: Request):
    current = _request_auth(request)
    if current.user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current


def _json_safe(value: Any) -> Any:
    try:
        import numpy as np

        if isinstance(value, (np.floating, np.integer)):
            value = value.item()
        if isinstance(value, np.bool_):
            return bool(value)
    except Exception:
        pass
    try:
        import math

        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
    except Exception:
        pass
    if hasattr(value, "isoformat"):
        return int(value.timestamp())
    return value


def _candles_to_json(df):
    payload = []
    for idx, row in df.iterrows():
        ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
        payload.append(
            {
                "time": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return payload


async def _scanner_loop():
    global _latest_scan_results, _last_scan_time
    while True:
        try:
            cfg = config_manager.reload()
            scan_cfg = cfg.get("scan", {})
            indicator_cfg = cfg.get("indicators", {})
            options_cfg = cfg.get("options", {})

            from .db import SessionLocal

            db = SessionLocal()
            try:
                configured_symbols = scan_cfg.get("symbols", [])
                ensure_watchlist_seeded(db, configured_symbols)
                symbols = get_watchlist_symbols(db)
                results = run_scan_for_symbols(db, symbols, scan_cfg, indicator_cfg, options_cfg)
                _latest_scan_results = {r["symbol"]: r for r in results}
                _last_scan_time = datetime.now(timezone.utc).isoformat()
            finally:
                db.close()
        except Exception:
            pass

        sleep_seconds = int(config_manager.get("scan", "sleep_seconds", default=300))
        await asyncio.sleep(max(30, sleep_seconds))


def _parse_backfill_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recount_backfill_run(db: Session, run: BackfillRun) -> None:
    run.chunks_completed = (
        db.query(BackfillChunk)
        .filter(BackfillChunk.run_id == run.id)
        .filter(BackfillChunk.status.in_(["COMPLETE", "SKIPPED"]))
        .count()
    )
    run.chunks_failed = (
        db.query(BackfillChunk)
        .filter(BackfillChunk.run_id == run.id)
        .filter(BackfillChunk.status == "FAILED")
        .count()
    )
    run.chunks_total = db.query(BackfillChunk).filter(BackfillChunk.run_id == run.id).count()


def _backfill_cancelled(db: Session, run_id: int) -> bool:
    run = db.query(BackfillRun).filter(BackfillRun.id == run_id).first()
    return bool(run and run.status == "CANCELLED")


def _sleep_backfill(seconds: float, run_id: int) -> None:
    if seconds <= 0:
        return
    deadline = time.time() + seconds
    while time.time() < deadline:
        db = SessionLocal()
        try:
            if _backfill_cancelled(db, run_id):
                return
        finally:
            db.close()
        time.sleep(min(1.0, max(0.0, deadline - time.time())))


def _fetch_backfill_chunk(symbol: str, interval: str, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], str, str | None]:
    selection = provider_factory.get_historical_candles_provider()
    try:
        return provider_factory.with_fallback(
            selection,
            "get_candles_range",
            symbol,
            interval,
            start.isoformat(),
            end.isoformat(),
        )
    except Exception as range_exc:
        if detect_rate_limit_error(range_exc):
            raise
        # Providers without ranged history support should use the smallest practical period instead of a huge backfill call.
        fallback_period = "1y" if interval == "1d" else "60d"
        candles, provider_name, warning = provider_factory.with_fallback(
            selection,
            "get_candles",
            symbol,
            interval,
            fallback_period,
        )
        message = f"Ranged candle request unavailable: {range_exc}. Fetched {fallback_period} instead."
        return candles, provider_name, f"{warning} | {message}" if warning else message


def _run_backfill_sync(run_id: int) -> None:
    history_cfg = config_manager.get("history", default={}) or {}
    skip_existing = bool(history_cfg.get("skip_existing_ranges", True))
    symbol_throttle = float(history_cfg.get("throttle_seconds_between_symbols", 5) or 5)
    interval_throttle = float(history_cfg.get("throttle_seconds_between_intervals", 10) or 10)

    db = SessionLocal()
    try:
        run = db.query(BackfillRun).filter(BackfillRun.id == run_id).first()
        if not run:
            return
        run.status = "RUNNING"
        run.message = run.message or "Backfill running"
        stale_chunks = (
            db.query(BackfillChunk)
            .filter(BackfillChunk.run_id == run_id)
            .filter(BackfillChunk.status == "RUNNING")
            .all()
        )
        for stale_chunk in stale_chunks:
            stale_chunk.status = "PENDING"
            stale_chunk.error_message = "Reset stale RUNNING chunk for resume"
            stale_chunk.started_at = None
            stale_chunk.finished_at = None
        db.commit()

        chunks = (
            db.query(BackfillChunk)
            .filter(BackfillChunk.run_id == run_id)
            .filter(BackfillChunk.status.in_(["PENDING", "FAILED"]))
            .order_by(BackfillChunk.symbol, BackfillChunk.interval, BackfillChunk.start_timestamp)
            .all()
        )

        for index, chunk in enumerate(chunks):
            db.refresh(run)
            if run.status == "CANCELLED":
                run.finished_at = now_iso()
                db.commit()
                return

            historical_provider = provider_factory.get_historical_candles_provider().primary.name
            cooldown = provider_cooldown(historical_provider, db=db)
            if cooldown.get("active"):
                remaining = int(float(cooldown.get("remaining_seconds") or 0))
                _recount_backfill_run(db, run)
                run.status = "FAILED"
                run.finished_at = now_iso()
                run.message = (
                    f"Backfill paused because {historical_provider} is cooling down for "
                    f"{remaining} seconds after a rate-limit response. Resume after cooldown; "
                    "completed chunks will be skipped."
                )
                db.commit()
                return

            start = _parse_backfill_dt(chunk.start_timestamp)
            end = _parse_backfill_dt(chunk.end_timestamp)
            chunk.status = "RUNNING"
            chunk.started_at = now_iso()
            chunk.finished_at = None
            chunk.error_message = None
            run.message = f"Backfilling {chunk.symbol} {chunk.interval}"
            db.commit()

            try:
                if skip_existing and range_coverage_complete(chunk.symbol, chunk.interval, start, end, db):
                    chunk.status = "SKIPPED"
                    chunk.finished_at = now_iso()
                    chunk.rows_inserted = 0
                    chunk.rows_updated = 0
                    run.message = f"Skipped existing range for {chunk.symbol} {chunk.interval}"
                else:
                    fetch_start = start
                    coverage = stored_range(chunk.symbol, chunk.interval, start, end, db)
                    if coverage["count"] and coverage["max_ts"]:
                        candidate_start = datetime.fromtimestamp(
                            int(coverage["max_ts"]) + interval_seconds(chunk.interval),
                            timezone.utc,
                        )
                        if start < candidate_start < end:
                            fetch_start = candidate_start
                    candles, provider_name, warning = _fetch_backfill_chunk(chunk.symbol, chunk.interval, fetch_start, end)
                    inserted, updated = upsert_candles(chunk.symbol, chunk.interval, candles, provider_name)
                    chunk.status = "COMPLETE"
                    chunk.provider = provider_name
                    chunk.rows_inserted = inserted
                    chunk.rows_updated = updated
                    chunk.finished_at = now_iso()
                    run.rows_inserted = int(run.rows_inserted or 0) + inserted
                    run.rows_updated = int(run.rows_updated or 0) + updated
                    if warning:
                        run.message = warning
                    elif candles and start.timestamp() < min(int(row.get("time", 0) or 0) for row in candles):
                        run.message = (
                            f"Provider only returned partial {chunk.interval} history for {chunk.symbol}. "
                            "Stored available data and will continue building history from live scans."
                        )
                    else:
                        run.message = f"Stored {inserted} inserted / {updated} updated rows for {chunk.symbol} {chunk.interval}"
            except Exception as exc:
                chunk.status = "FAILED"
                chunk.error_message = str(exc)[:2000]
                chunk.finished_at = now_iso()
                run.error_count = int(run.error_count or 0) + 1
                run.message = f"Backfill chunk failed for {chunk.symbol} {chunk.interval}: {exc}"
                if detect_rate_limit_error(exc):
                    _recount_backfill_run(db, run)
                    run.status = "FAILED"
                    run.finished_at = now_iso()
                    run.message = (
                        f"Backfill paused because provider rate limit was reached while fetching "
                        f"{chunk.symbol} {chunk.interval}. Resume later; completed chunks will be skipped."
                    )
                    db.commit()
                    return

            _recount_backfill_run(db, run)
            db.commit()

            if index < len(chunks) - 1:
                next_chunk = chunks[index + 1]
                delay = interval_throttle if next_chunk.interval != chunk.interval else symbol_throttle
                run.message = f"Throttling {delay:g}s before next backfill chunk"
                db.commit()
                _sleep_backfill(delay, run_id)

        db.refresh(run)
        if run.status != "CANCELLED":
            _recount_backfill_run(db, run)
            run.status = "FAILED" if int(run.chunks_failed or 0) else "COMPLETE"
            run.finished_at = now_iso()
            if not run.message or run.message.startswith("Throttling"):
                run.message = "Backfill complete" if run.status == "COMPLETE" else "Backfill completed with failed chunks"
            try:
                symbols = json.loads(run.symbols or "[]")
            except Exception:
                symbols = []
            for symbol in symbols:
                profile = db.query(TickerProfile).filter(TickerProfile.symbol == str(symbol).upper()).first()
                if profile:
                    evaluate_profile_completeness(db, profile, persist=True)
            db.commit()
    finally:
        db.close()


async def _run_backfill(run_id: int) -> None:
    await asyncio.to_thread(_run_backfill_sync, run_id)


async def _run_trade_review_sync(run_id: int, payload: dict[str, Any] | None = None) -> None:
    db = SessionLocal()
    try:
        await asyncio.to_thread(run_trade_review_sync, db, run_id, payload or {})
    finally:
        db.close()


def _pause_stale_backfill_runs() -> None:
    db = SessionLocal()
    try:
        stale_runs = db.query(BackfillRun).filter(BackfillRun.status == "RUNNING").all()
        for run in stale_runs:
            running_chunks = (
                db.query(BackfillChunk)
                .filter(BackfillChunk.run_id == run.id)
                .filter(BackfillChunk.status == "RUNNING")
                .all()
            )
            for chunk in running_chunks:
                chunk.status = "PENDING"
                chunk.error_message = "Reset after backend restart"
                chunk.started_at = None
                chunk.finished_at = None
            run.status = "FAILED"
            run.finished_at = now_iso()
            run.message = "Backfill paused because backend restarted. Resume to continue; completed chunks will be skipped."
            _recount_backfill_run(db, run)
        if stale_runs:
            db.commit()
    finally:
        db.close()


@app.on_event("startup")
async def startup_event():
    global _earnings_profile_task
    _pause_stale_backfill_runs()
    db = SessionLocal()
    try:
        pause_trade_review_stale_runs(db)
        ensure_core_universe(db)
    finally:
        db.close()
    # Slow Yahoo prefetch worker improves cache hit rate and avoids bursty on-demand calls.
    start_candle_worker()
    if _earnings_profile_task is None or _earnings_profile_task.done():
        _earnings_profile_task = asyncio.create_task(_refresh_missing_earnings_profiles())
    if etrade_auth.is_connected():
        with contextlib.suppress(Exception):
            get_open_option_positions(refresh=True, market_session=get_market_session())
    start_option_estimation_worker()
    start_active_signal_worker()
    return


def _refresh_one_missing_earnings_profile(symbol: str) -> None:
    db = SessionLocal()
    try:
        profile = db.query(TickerProfile).filter(TickerProfile.symbol == symbol).first()
        if not profile:
            return
        try:
            stats = json.loads(profile.stats_json or "{}")
        except Exception:
            stats = {}
        if "earnings_history" not in stats:
            refresh_ticker_profile(db, symbol, source="earnings_history_backfill")
        elif "social_history" not in stats:
            stats["social_history"] = build_social_profile(symbol, db)
            profile.stats_json = json.dumps(stats, sort_keys=True)
            profile.last_profile_update_at = now_iso()
            profile.updated_at = now_iso()
        else:
            return
        evaluate_profile_completeness(db, profile, persist=True)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


async def _refresh_missing_earnings_profiles() -> None:
    """Backfill the new profile field without delaying application startup."""
    db = SessionLocal()
    try:
        symbols = ensure_core_universe(db)
        existing_profiles = [row.symbol for row in db.query(TickerProfile).all()]
        for symbol in existing_profiles:
            if symbol not in symbols:
                symbols.append(symbol)
    finally:
        db.close()
    for symbol in symbols:
        await asyncio.to_thread(_refresh_one_missing_earnings_profile, symbol)


@app.on_event("shutdown")
async def shutdown_event():
    await stop_candle_worker()
    await stop_option_estimation_worker()
    await stop_active_signal_worker()
    return


@app.get("/api/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        timestamp=datetime.now(timezone.utc).isoformat(),
        config_loaded=config_manager.loaded,
        config_error=config_manager.error,
    )


@app.get("/api/config")
def get_config():
    return config_manager.config


@app.get("/api/cache/candles/status")
def candles_cache_status():
    return candle_worker_status()


@app.get("/api/providers/status")
def providers_status():
    return {"providers": provider_status()}


@app.get("/api/market/session")
def market_session_status():
    return get_market_session()


@app.get("/api/options/estimates")
def option_estimates(request: Request, symbol: str | None = None, limit: int = 100, db: Session = Depends(get_db)):
    _request_admin(request)
    return latest_estimates(db, symbol=symbol, limit=limit)


@app.get("/api/options/estimates/status")
def option_estimates_status(request: Request):
    _request_admin(request)
    return option_estimation_status()


@app.get("/api/db/status")
def get_db_status():
    return history_db_status()


@app.get("/api/news/rss")
def get_market_news():
    return market_news_feed()


@app.get("/api/news/earnings")
def get_upcoming_earnings():
    return upcoming_earnings_feed()


@app.get("/api/news/catalyst/{symbol}")
def get_news_catalyst(
    symbol: str,
    historical: bool = False,
    direction: str | None = None,
    entry_ts: str | None = None,
    exit_ts: str | None = None,
    expiration: str | None = None,
    context_type: str = "candidate",
):
    return build_news_catalyst_impact(
        symbol,
        market_session=get_market_session(),
        historical=historical,
        direction=direction,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        expiration=expiration,
        context_type=context_type,
    )


def _serialize_backfill_run(run: BackfillRun | None, db: Session) -> dict[str, Any] | None:
    if not run:
        return None
    chunks_completed = (
        db.query(BackfillChunk)
        .filter(BackfillChunk.run_id == run.id)
        .filter(BackfillChunk.status.in_(["COMPLETE", "SKIPPED"]))
        .count()
    )
    chunks_failed = (
        db.query(BackfillChunk)
        .filter(BackfillChunk.run_id == run.id)
        .filter(BackfillChunk.status == "FAILED")
        .count()
    )
    chunks_total = db.query(BackfillChunk).filter(BackfillChunk.run_id == run.id).count()
    running_chunk = (
        db.query(BackfillChunk)
        .filter(BackfillChunk.run_id == run.id)
        .filter(BackfillChunk.status == "RUNNING")
        .order_by(desc(BackfillChunk.started_at))
        .first()
    )
    return {
        "id": run.id,
        "status": run.status,
        "symbols": run.symbols,
        "intervals": run.intervals,
        "period": run.period,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "rows_inserted": run.rows_inserted,
        "rows_updated": run.rows_updated,
        "chunks_total": chunks_total,
        "chunks_completed": chunks_completed,
        "chunks_failed": chunks_failed,
        "error_count": run.error_count,
        "message": run.message,
        "current_symbol": running_chunk.symbol if running_chunk else None,
        "current_interval": running_chunk.interval if running_chunk else None,
        "current_provider": running_chunk.provider if running_chunk else None,
    }


@app.post("/api/history/backfill")
async def start_history_backfill(payload: dict[str, Any] | None = None):
    global _backfill_task
    if not bool(config_manager.get("history", "enabled", default=True)):
        raise HTTPException(status_code=400, detail="Historical backfill is disabled")
    historical_provider = provider_factory.get_historical_candles_provider().primary.name
    cooldown = provider_cooldown(historical_provider)
    if cooldown.get("active"):
        remaining = int(float(cooldown.get("remaining_seconds") or 0))
        return {
            "ok": True,
            "started": False,
            "status": "COOLDOWN",
            "provider": historical_provider,
            "cooldown": cooldown,
            "message": (
                f"Backfill not started because {historical_provider} is cooling down for "
                f"{remaining} seconds after a rate-limit response."
            ),
        }
    run = create_or_resume_backfill_run(payload or {})
    if _backfill_task is None or _backfill_task.done():
        _backfill_task = asyncio.create_task(_run_backfill(run.id))
    return {"ok": True, "started": True, "run_id": run.id, "status": run.status}


async def _queue_history_backfill(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    global _backfill_task
    if not bool(config_manager.get("history", "enabled", default=True)):
        return {"ok": False, "started": False, "status": "DISABLED", "message": "Historical backfill is disabled"}
    historical_provider = provider_factory.get_historical_candles_provider().primary.name
    cooldown = provider_cooldown(historical_provider)
    if cooldown.get("active"):
        remaining = int(float(cooldown.get("remaining_seconds") or 0))
        return {
            "ok": True,
            "started": False,
            "status": "COOLDOWN",
            "provider": historical_provider,
            "cooldown": cooldown,
            "message": f"Backfill not started because {historical_provider} is cooling down for {remaining} seconds.",
        }
    run = create_or_resume_backfill_run(payload or {})
    if _backfill_task is None or _backfill_task.done():
        _backfill_task = asyncio.create_task(_run_backfill(run.id))
        started = True
    else:
        started = False
    return {"ok": True, "started": started, "run_id": run.id, "status": run.status}


@app.post("/api/history/setup-match/backfill")
async def start_setup_match_backfill(payload: dict[str, Any] | None = None):
    cfg = config_manager.get("historical_patterns", default={}) or {}
    data = dict(payload or {})
    data.setdefault("all_symbols", True)
    data.setdefault("period", cfg.get("backfill_period", "3y"))
    data.setdefault("intervals", cfg.get("backfill_intervals", ["15m", "1d"]))
    data.setdefault("max_symbols", 0)
    return await _queue_history_backfill(data)


@app.get("/api/history/setup-match/{symbol}")
async def get_historical_setup_match(
    symbol: str,
    side: str | None = None,
    interval: str | None = None,
    period: str | None = None,
    ensure_backfill: bool = True,
    include_contracts: bool = True,
):
    cfg = config_manager.get("historical_patterns", default={}) or {}
    normalized = normalize_symbol(symbol)
    backfill_request = None
    if ensure_backfill:
        backfill_request = await _queue_history_backfill(
            {
                "symbols": [normalized],
                "period": cfg.get("backfill_period", "3y"),
                "intervals": cfg.get("backfill_intervals", ["15m", "1d"]),
                "max_symbols": 1,
            }
        )
    payload = await asyncio.to_thread(
        build_historical_setup_match,
        normalized,
        side=side,
        interval=interval,
        period=period,
        include_contracts=include_contracts,
    )
    payload["background_backfill"] = backfill_request
    return payload


@app.get("/api/history/backfill/status")
def history_backfill_status(db: Session = Depends(get_db)):
    active = (
        db.query(BackfillRun)
        .filter(BackfillRun.status.in_(["PENDING", "RUNNING", "FAILED"]))
        .order_by(desc(BackfillRun.started_at))
        .first()
    )
    latest = active or db.query(BackfillRun).order_by(desc(BackfillRun.started_at)).first()
    serialized = _serialize_backfill_run(latest, db)
    last_error = latest_provider_error(db)
    history_cfg = config_manager.get("history", default={}) or {}
    return {
        "active_run": serialized,
        "running": bool(active and active.status == "RUNNING"),
        "chunks_complete": int((serialized.get("chunks_completed") if serialized else 0) or 0),
        "chunks_total": int((serialized.get("chunks_total") if serialized else 0) or 0),
        "failed_chunks": int((serialized.get("chunks_failed") if serialized else 0) or 0),
        "rows_inserted": int((latest.rows_inserted if latest else 0) or 0),
        "rows_updated": int((latest.rows_updated if latest else 0) or 0),
        "current_symbol": None if not serialized else serialized.get("current_symbol"),
        "current_interval": None if not serialized else serialized.get("current_interval"),
        "current_throttle_delay": {
            "between_symbols": history_cfg.get("throttle_seconds_between_symbols", 5),
            "between_intervals": history_cfg.get("throttle_seconds_between_intervals", 10),
        },
        "last_provider_error": None
        if not last_error
        else {
            "provider": last_error.provider,
            "symbol": last_error.symbol,
            "endpoint": last_error.endpoint,
            "error_message": last_error.error_message,
            "error_type": last_error.error_type,
            "created_at": last_error.created_at,
        },
    }


@app.post("/api/history/backfill/cancel")
def cancel_history_backfill(db: Session = Depends(get_db)):
    run = (
        db.query(BackfillRun)
        .filter(BackfillRun.status.in_(["PENDING", "RUNNING", "FAILED"]))
        .order_by(desc(BackfillRun.started_at))
        .first()
    )
    if not run:
        return {"ok": True, "cancelled": False, "message": "No active backfill run"}
    run.status = "CANCELLED"
    run.finished_at = now_iso()
    run.message = "Backfill cancelled"
    running_chunks = (
        db.query(BackfillChunk)
        .filter(BackfillChunk.run_id == run.id)
        .filter(BackfillChunk.status == "RUNNING")
        .all()
    )
    for chunk in running_chunks:
        chunk.status = "FAILED"
        chunk.error_message = "Backfill cancelled"
        chunk.finished_at = now_iso()
    db.commit()
    return {"ok": True, "cancelled": True, "run_id": run.id}


def _parse_trade_review_filters(
    account_ref: str | None = None,
    account_refs: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    ticker: str | None = None,
    call_put: str | None = None,
    winner_loser: str | None = None,
    grade: str | None = None,
    dte_bucket: str | None = None,
    setup_type: str | None = None,
    market_regime: str | None = None,
    reviewed: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    refs = [str(account_ref).strip()] if account_ref else []
    if account_refs:
        refs.extend([item.strip() for item in account_refs.split(",") if item.strip()])
    reviewed_value: bool | None = None
    if reviewed is not None:
        text = str(reviewed).strip().lower()
        if text in {"true", "1", "yes", "y"}:
            reviewed_value = True
        elif text in {"false", "0", "no", "n"}:
            reviewed_value = False
    return {
        "account_refs": [ref for ref in refs if ref],
        "from_date": from_date,
        "to_date": to_date,
        "ticker": ticker,
        "call_put": call_put,
        "winner_loser": winner_loser,
        "grade": grade,
        "dte_bucket": dte_bucket,
        "setup_type": setup_type,
        "market_regime": market_regime,
        "reviewed": reviewed_value,
        "limit": limit if limit and limit > 0 else None,
        "offset": offset if offset and offset > 0 else 0,
    }


@app.get("/api/trade-review/accounts")
def trade_review_accounts(request: Request, db: Session = Depends(get_db)):
    current = _request_admin(request)
    refresh_requested = str(request.query_params.get("refresh") or "").strip().lower() in {"1", "true", "yes"}
    if refresh_requested:
        refresh_trade_review_accounts(db, current.user.username)
    accounts = reviewable_accounts(db)
    selection = get_trade_review_selection(db, current.user.username)
    return {
        "selection": selection,
        "accounts": [
            {
                "account_ref": account.account_ref,
                "account_mask": account.account_mask,
                "account_desc": account.account_desc,
                "account_name": account.account_name,
                "account_type": account.account_type,
                "account_mode": account.account_mode,
                "institution_type": account.institution_type,
                "selected": account.account_ref in set(selection.get("selected_account_refs") or []),
                "last_sync_status": account.last_sync_status,
                "last_error_message": account.last_error_message,
                "last_successful_sync_at": account.last_successful_sync_at,
                "oldest_available_history_at": account.oldest_available_history_at,
            }
            for account in accounts
        ],
        "sync": serialize_trade_review_sync_run(db, get_trade_review_active_run(db)),
    }


@app.post("/api/trade-review/accounts/refresh")
def trade_review_refresh_accounts(request: Request, db: Session = Depends(get_db)):
    current = _request_admin(request)
    try:
        accounts = refresh_trade_review_accounts(db, current.user.username)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "ok": True,
        "accounts": [
            {
                "account_ref": account["account_ref"],
                "account_mask": account["account_mask"],
                "account_desc": account["account_desc"],
                "account_type": account["account_type"],
                "institution_type": account["institution_type"],
            }
            for account in accounts
        ],
    }


@app.post("/api/trade-review/accounts/selection")
def trade_review_update_selection(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_admin(request)
    data = payload or {}
    selection_mode = str(data.get("selection_mode") or "EXPLICIT")
    account_refs = data.get("account_refs") or []
    if not isinstance(account_refs, list):
        account_refs = [account_refs]
    try:
        selection = set_trade_review_selection(db, current.user.username, selection_mode, [str(ref) for ref in account_refs])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "selection": selection}


@app.post("/api/trade-review/sync")
async def trade_review_start_sync(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    global _trade_review_task
    current = _request_admin(request)
    data = payload or {}
    if "selection_mode" in data or "account_refs" in data:
        account_refs = data.get("account_refs") or []
        if not isinstance(account_refs, list):
            account_refs = [account_refs]
        try:
            set_trade_review_selection(db, current.user.username, str(data.get("selection_mode") or "EXPLICIT"), [str(ref) for ref in account_refs])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if bool(data.get("refresh_accounts", True)):
        try:
            refresh_trade_review_accounts(db, current.user.username)
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    selected_accounts, selection_preview = _resolve_accounts_for_user(db, current.user.username)
    if not selected_accounts:
        raise HTTPException(status_code=400, detail="Select one or more accounts before importing history.")

    active = get_trade_review_active_run(db)
    if active and active.status == "RUNNING":
        return {
            "ok": True,
            "started": False,
            "run": serialize_trade_review_sync_run(db, active),
            "message": "A trade review sync is already running",
        }

    if active and active.status in {"PENDING", "FAILED"}:
        run = active
        if data.get("from_date"):
            run.from_date = str(data.get("from_date") or "") or None
        if data.get("to_date"):
            run.to_date = str(data.get("to_date") or "") or None
        run.updated_at = now_iso()
        db.commit()
    else:
        selection = get_trade_review_selection(db, current.user.username)
        try:
            run = TradeReviewSyncRun(
                username=current.user.username,
                selection_mode=str(selection.get("selection_mode") or "EXPLICIT"),
                selected_account_refs=json.dumps(selection.get("selected_account_refs") or []),
                status="PENDING",
                from_date=str(data.get("from_date") or "") or None,
                to_date=str(data.get("to_date") or "") or None,
                created_at=now_iso(),
                started_at=now_iso(),
                updated_at=now_iso(),
                current_stage="pending",
                current_message="Queued trade review sync",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if _trade_review_task is None or _trade_review_task.done():
        _trade_review_task = asyncio.create_task(_run_trade_review_sync(run.id, {"username": current.user.username, **data}))
    return {"ok": True, "started": True, "run_id": run.id, "run": serialize_trade_review_sync_run(db, run)}


@app.get("/api/trade-review/status")
def trade_review_status(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    active = get_trade_review_active_run(db)
    return {
        "active_run": serialize_trade_review_sync_run(db, active),
        "running": bool(active and active.status == "RUNNING"),
        "last_error": active.last_error if active else None,
    }


@app.post("/api/trade-review/cancel")
def trade_review_cancel(request: Request, db: Session = Depends(get_db)):
    current = _request_admin(request)
    return cancel_trade_review_sync(db, current.user.username)


@app.get("/api/trade-review/overview")
def trade_review_overview(
    request: Request,
    account_ref: str | None = None,
    account_refs: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    ticker: str | None = None,
    call_put: str | None = None,
    winner_loser: str | None = None,
    grade: str | None = None,
    dte_bucket: str | None = None,
    setup_type: str | None = None,
    market_regime: str | None = None,
    reviewed: str | None = None,
    limit: int = 250,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    current = _request_admin(request)
    filters = _parse_trade_review_filters(
        account_ref=account_ref,
        account_refs=account_refs,
        from_date=from_date,
        to_date=to_date,
        ticker=ticker,
        call_put=call_put,
        winner_loser=winner_loser,
        grade=grade,
        dte_bucket=dte_bucket,
        setup_type=setup_type,
        market_regime=market_regime,
        reviewed=reviewed,
        limit=limit,
        offset=offset,
    )
    overview = build_trade_review_overview(db, current.user.username, filters)
    if offset > 0:
        overview["trades"] = overview.get("trades", [])[offset:]
    overview["available_filters"] = list_trade_review_filters(db, current.user.username)
    return overview


@app.get("/api/trade-review/trades/{trade_id}")
def trade_review_trade_detail(request: Request, trade_id: int, refresh_context: bool = False, include_analysis: bool = True, db: Session = Depends(get_db)):
    _request_admin(request)
    return build_trade_review_detail(db, trade_id, refresh_context=refresh_context, include_analysis=include_analysis)


@app.get("/api/trade-review/trades/{trade_id}/analysis")
def trade_review_trade_analysis(request: Request, trade_id: int, db: Session = Depends(get_db)):
    _request_admin(request)
    detail = build_trade_review_detail(db, trade_id, refresh_context=False, include_analysis=True)
    return detail.get("analysis") or {}


@app.patch("/api/trade-review/trades/{trade_id}")
def trade_review_update_trade(request: Request, trade_id: int, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_admin(request)
    data = payload or {}
    trade = db.query(TradeReviewTrade).filter(TradeReviewTrade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if "reviewed" in data:
        trade.reviewed = bool(data.get("reviewed"))
        trade.reviewed_at = now_iso() if trade.reviewed else None
    if "admin_notes" in data:
        trade.admin_notes = str(data.get("admin_notes") or "").strip() or None
    trade.updated_at = now_iso()
    db.commit()
    _audit(db, current.user.username, "update_trade", "trade", str(trade_id), "Updated reviewed flag or admin notes")
    return {"ok": True, "trade_id": trade_id}


@app.get("/api/auth/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    current = site_auth.current_auth(db, request)
    return site_auth.status(db, request=request, current=current)


@app.post("/api/auth/login")
def auth_login(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    username = str((payload or {}).get("username") or "").strip()
    password = str((payload or {}).get("password") or "")
    return site_auth.login(db, request, username=username, password=password)


@app.post("/api/auth/logout")
def auth_logout(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("authorization", "").split(" ", 1)[1].strip() if request.headers.get("authorization", "").lower().startswith("bearer ") else request.headers.get("x-auth-token", "").strip()
    if token:
        site_auth.revoke_token(db, token)
    return {"ok": True}


@app.post("/api/auth/change-password")
def auth_change_password(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_auth(request)
    data = payload or {}
    new_password = str(data.get("new_password") or "")
    updated_user = site_auth.change_password(db, current, new_password=new_password)
    return {"ok": True, "user": updated_user}


@app.get("/api/auth/me")
def auth_me(request: Request, db: Session = Depends(get_db)):
    current = _request_auth(request)
    return site_auth.status(db, request=request, current=current)


@app.post("/api/auth/bootstrap")
def auth_bootstrap(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    data = payload or {}
    setup_key = str(data.get("setup_key") or "")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    role = str(data.get("role") or "admin").strip().lower()
    created = site_auth.bootstrap_user(db, setup_key=setup_key, username=username, password=password, role=role)
    login_result = site_auth.login(db, request, username=username, password=password)
    return {"ok": True, "created_user": created, "session": login_result}


@app.get("/api/auth/users")
def auth_users(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    return {"users": site_auth.list_users(db)}


@app.post("/api/auth/users")
def auth_create_user(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    _request_admin(request)
    data = payload or {}
    created = site_auth.create_user(
        db,
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
        role=str(data.get("role") or "user"),
        created_by=_request_auth(request).user.username,
    )
    return {"ok": True, "user": created}


@app.patch("/api/auth/users/{username}")
def auth_update_user(username: str, request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    _request_admin(request)
    data = payload or {}
    updated = site_auth.update_user(
        db,
        username,
        password=data.get("password"),
        role=data.get("role"),
        active=data.get("active"),
    )
    return {"ok": True, "user": updated}


@app.get("/api/auth/blocked-ips")
def auth_blocked_ips(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    return {"blocked_ips": site_auth.list_blocked_ips(db)}


@app.delete("/api/auth/blocked-ips/{ip}")
def auth_unblock_ip(ip: str, request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    site_auth.unblock_ip(db, ip)
    return {"ok": True, "ip_address": ip}


@app.get("/api/auth/etrade/status")
def etrade_status():
    return etrade_auth.status()


@app.get("/api/auth/etrade/connect")
def etrade_connect():
    try:
        return etrade_auth.start_connect()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/auth/etrade/callback")
def etrade_callback(oauth_verifier: str | None = None):
    if not oauth_verifier:
        raise HTTPException(status_code=400, detail="Missing oauth_verifier")
    try:
        etrade_auth.finish_connect(oauth_verifier)
        return HTMLResponse(
            """
            <html><body style='font-family: sans-serif; background:#0b1220; color:#d8e1f0;'>
            <h2>E*TRADE Connected</h2>
            <p>You can close this window and return to the dashboard settings page.</p>
            </body></html>
            """
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auth/etrade/verify")
def etrade_verify(payload: dict[str, str]):
    oauth_verifier = (payload or {}).get("oauth_verifier", "").strip()
    if not oauth_verifier:
        raise HTTPException(status_code=400, detail="Missing oauth_verifier")
    try:
        etrade_auth.finish_connect(oauth_verifier)
        return {"ok": True, "message": "E*TRADE connected"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auth/etrade/disconnect")
def etrade_disconnect():
    etrade_auth.disconnect()
    return {"ok": True}


@app.get("/api/admin/etrade/positions")
def admin_etrade_positions(request: Request, refresh: bool = False):
    _request_admin(request)
    return get_open_option_positions(refresh=refresh, market_session=get_market_session())


@app.get("/api/etrade/positions")
def etrade_positions(request: Request, refresh: bool = False):
    _request_admin(request)
    return get_open_option_positions(refresh=refresh, market_session=get_market_session())


@app.get("/api/etrade/accounts")
def etrade_accounts(request: Request, refresh: bool = False):
    _request_admin(request)
    payload = get_open_option_positions(refresh=refresh, market_session=get_market_session())
    return {"accounts": payload.get("accounts") or [], "provider": "etrade", "source": "real_brokerage_only"}


@app.get("/api/etrade/orders")
def etrade_orders(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    rows = db.query(BrokerageOrder).filter(BrokerageOrder.broker == "etrade").order_by(BrokerageOrder.id.desc()).limit(500).all()
    return {"orders": [{"broker_record_id": row.broker_record_id, "status": row.status, "broker_timestamp": row.broker_timestamp, "broker": row.broker} for row in rows], "source": "real_brokerage_only"}


@app.get("/api/etrade/trades")
def etrade_trades(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    fills = db.query(BrokerageFill).filter(BrokerageFill.broker == "etrade").order_by(BrokerageFill.id.desc()).limit(500).all()
    transactions = db.query(BrokerageTransaction).filter(BrokerageTransaction.broker == "etrade").order_by(BrokerageTransaction.id.desc()).limit(500).all()
    return {
        "fills": [{"broker_record_id": row.broker_record_id, "order_record_id": row.order_record_id, "symbol": row.symbol, "quantity": row.quantity, "fill_price": row.fill_price, "fees": row.fees, "broker_timestamp": row.broker_timestamp} for row in fills],
        "transactions": [{"broker_record_id": row.broker_record_id, "transaction_type": row.transaction_type, "symbol": row.symbol, "amount": row.amount, "broker_timestamp": row.broker_timestamp} for row in transactions],
        "source": "real_brokerage_only",
    }


@app.get("/api/paper/portfolio")
def paper_portfolio(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    return get_paper_portfolio(db, market_session=get_market_session())


@app.get("/api/paper/morning-brief")
def paper_morning_brief(request: Request, db: Session = Depends(get_db)):
    """Return the cached-data morning plan. This route cannot place orders."""
    _request_admin(request)
    return build_morning_brief(db, market_session=get_market_session())


@app.post("/api/paper/morning-brief/refresh")
def refresh_paper_morning_brief(request: Request, db: Session = Depends(get_db)):
    """Rebuild the paper-only watchlist from stored provider data."""
    _request_admin(request)
    return build_morning_brief(db, market_session=get_market_session(), refresh=True)


@app.post("/api/paper/morning-candidates/{candidate_id}/outcome")
def paper_morning_candidate_outcome(candidate_id: int, request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    """Append an outcome to an immutable morning snapshot; never changes its original payload."""
    _request_admin(request)
    row = db.query(PaperMorningCandidate).filter(PaperMorningCandidate.id == candidate_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Morning candidate not found")
    data = payload or {}
    outcome = str(data.get("outcome") or "").upper()
    allowed = {"TRIGGERED", "SKIPPED", "EXTENDED", "INVALIDATED", "TARGET_1", "TARGET_2", "MISSED", "NO_TRADE"}
    if outcome not in allowed:
        raise HTTPException(status_code=400, detail=f"Outcome must be one of: {', '.join(sorted(allowed))}")
    row.triggered = bool(data.get("triggered", outcome == "TRIGGERED"))
    row.outcome = outcome
    row.outcome_json = json.dumps(data, sort_keys=True)
    row.updated_at = now_iso()
    db.commit()
    return {"id": row.id, "morning_date": row.morning_date, "symbol": row.symbol, "triggered": row.triggered, "outcome": row.outcome, "snapshot_immutable": True}


@app.get("/api/paper/positions")
def paper_positions(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    payload = get_paper_portfolio(db, market_session=get_market_session())
    return {"positions": payload.get("positions") or [], "source": "paper_tables_only"}


@app.get("/api/paper/orders")
def paper_orders(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    payload = get_paper_portfolio(db, market_session=get_market_session())
    return {"orders": payload.get("orders") or [], "fills": payload.get("fills") or [], "source": "paper_tables_only"}


@app.get("/api/paper/recommendations")
def paper_recommendations(request: Request, limit: int = 100, db: Session = Depends(get_db)):
    _request_admin(request)
    return {"recommendations": list_recommendations(db, limit=limit), "source": "paper_tables_only"}


@app.get("/api/paper/performance")
def paper_performance(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    return get_recommendation_performance(db)


@app.post("/api/paper/orders")
def paper_order(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_admin(request)
    try:
        return create_paper_order(db, payload or {}, current.user.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/paper/migration-review")
def paper_migration_review(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    rows = db.query(PaperMigrationReview).order_by(PaperMigrationReview.id.desc()).limit(500).all()
    return {
        "records": [
            {"id": row.id, "source_table": row.source_table, "source_record_id": row.source_record_id, "reason": row.reason, "status": row.status, "created_at": row.created_at}
            for row in rows
        ],
        "source": "migration_audit_only",
    }


@app.get("/api/watchlist")
def get_watchlist(db: Session = Depends(get_db)):
    rows = db.query(Watchlist).filter(Watchlist.active.is_(True)).all()
    return {"symbols": [r.symbol for r in rows]}


@app.get("/api/dashboard/decision")
def dashboard_decision(db: Session = Depends(get_db)):
    return build_decision_dashboard(db)


@app.get("/api/signals/active")
def active_signals(request: Request, refresh: bool = False, db: Session = Depends(get_db)):
    _request_auth(request)
    return get_active_signals(db, refresh=refresh)


@app.post("/api/signals/refresh")
def refresh_active_signals(request: Request, db: Session = Depends(get_db)):
    _request_auth(request)
    return {"status": "ok", **reconcile_signals(db)}


@app.post("/api/signals/{signal_id}/trigger")
def confirm_active_signal(signal_id: str, request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    _request_auth(request)
    try:
        return trigger_signal(db, signal_id, payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/signals/history")
def signal_history(request: Request, limit: int = 100, db: Session = Depends(get_db)):
    _request_auth(request)
    return get_signal_history(db, limit=limit)


@app.post("/api/signals/{signal_id}/outcome")
def active_signal_outcome(signal_id: str, request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    _request_admin(request)
    try:
        return record_signal_outcome(db, signal_id, payload or {})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/signals/status")
def active_signal_status(request: Request):
    _request_auth(request)
    return signal_worker_status()


@app.get("/api/recommendations/performance")
def recommendation_performance(db: Session = Depends(get_db)):
    return get_recommendation_performance(db)


@app.get("/api/recommendations")
def recommendations(request: Request, limit: int = 100, db: Session = Depends(get_db)):
    _request_admin(request)
    return {"recommendations": list_recommendations(db, limit=limit)}


@app.post("/api/recommendations/{recommendation_id}/trigger")
def recommendation_trigger(recommendation_id: str, request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_admin(request)
    try:
        record = trigger_recommendation(db, recommendation_id, payload or {}, current.user.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "recommendation_id": record.recommendation_id, "status": record.status, "triggered_at": record.triggered_at}


@app.post("/api/recommendations/{recommendation_id}/resolve")
def recommendation_resolve(recommendation_id: str, request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_admin(request)
    try:
        record = resolve_recommendation(db, recommendation_id, payload or {}, current.user.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "recommendation_id": record.recommendation_id, "status": record.status, "outcome": record.outcome, "resolved_at": record.resolved_at}


@app.get("/api/watchlist/intelligence")
def watchlist_intelligence(db: Session = Depends(get_db)):
    return build_watchlist_intelligence(db)


@app.post("/api/watchlist")
async def add_watchlist(item: WatchlistAddRequest, db: Session = Depends(get_db)):
    symbol = normalize_symbol(item.symbol)
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    row = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
    if row:
        row.active = True
        row.source = "user"
    else:
        row = Watchlist(
            symbol=symbol,
            source="user",
            active=True,
            added_at=datetime.now(timezone.utc).isoformat(),
        )
        db.add(row)
    profile = ensure_ticker_profile(db, symbol, source="watchlist_add")
    profile.profile_status = "BUILDING" if profile.profile_status in {"CREATED", "NOT_STARTED", None} else profile.profile_status
    profile.profile_state = "BUILDING" if profile.profile_state in {None, "NOT_STARTED"} else profile.profile_state
    profile.last_backfill_requested_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    profile = refresh_ticker_profile(db, symbol, source="watchlist_add")
    db.commit()
    backfill = None
    profile_cfg = config_manager.get("ticker_profiles", default={}) or {}
    if bool(profile_cfg.get("backfill_on_add", True)):
        backfill = await _queue_history_backfill(
            {
                "symbols": [symbol],
                "period": profile_cfg.get("backfill_period", "3y"),
                "intervals": profile_cfg.get("backfill_intervals", ["15m", "1d"]),
                "max_symbols": 1,
            }
        )
    return {"ok": True, "symbol": symbol, "profile": serialize_ticker_profile(profile), "backfill": backfill}


@app.delete("/api/watchlist/{symbol}")
def remove_watchlist(symbol: str, db: Session = Depends(get_db)):
    normalized = normalize_symbol(symbol)
    row = db.query(Watchlist).filter(Watchlist.symbol == normalized).first()
    if not row:
        raise HTTPException(status_code=404, detail="Symbol not in watchlist")
    row.active = False
    db.commit()
    return {"ok": True, "symbol": normalized}


@app.get("/api/ticker-profiles/{symbol}")
def ticker_profile(symbol: str, refresh: bool = False, db: Session = Depends(get_db)):
    normalized = normalize_symbol(symbol)
    profile = ensure_ticker_profile(db, normalized, source="profile_view")
    profile_stats = str(profile.stats_json or "")
    if refresh or not profile.last_profile_update_at or '"earnings_history"' not in profile_stats:
        profile = refresh_ticker_profile(db, normalized, source="profile_view")
    evaluate_profile_completeness(db, profile, market_session=get_market_session(), persist=True)
    db.commit()
    return serialize_ticker_profile(profile)


@app.post("/api/ticker-profiles/{symbol}/refresh")
async def refresh_profile(symbol: str, db: Session = Depends(get_db)):
    normalized = normalize_symbol(symbol)
    profile = refresh_ticker_profile(db, normalized, source="manual_refresh")
    profile.last_backfill_requested_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    profile_cfg = config_manager.get("ticker_profiles", default={}) or {}
    backfill = await _queue_history_backfill(
        {
            "symbols": [normalized],
            "period": profile_cfg.get("backfill_period", "3y"),
            "intervals": profile_cfg.get("backfill_intervals", ["15m", "1d"]),
            "max_symbols": 1,
        }
    )
    return {"ok": True, "profile": serialize_ticker_profile(profile), "backfill": backfill}


@app.get("/api/admin/advisory/settings")
def advisory_settings(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    return get_advisory_settings(db)


@app.get("/api/admin/social/settings")
def social_settings(request: Request, db: Session = Depends(get_db)):
    _request_admin(request)
    return get_social_settings(db)


@app.post("/api/admin/social/settings")
def update_admin_social_settings(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_admin(request)
    return update_social_settings(db, payload or {}, current.user.username)


@app.post("/api/admin/advisory/settings")
def update_admin_advisory_settings(request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    current = _request_admin(request)
    return get_advisory_settings(db) if payload is None else update_advisory_settings(db, payload, current.user.username)


@app.post("/api/advisory/{symbol}")
def advisory_for_symbol(symbol: str, request: Request, payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    _request_auth(request)
    data = payload or {}
    normalized = normalize_symbol(symbol)
    package = build_advisory_package(
        db,
        normalized,
        side=data.get("side"),
        candidate_id=data.get("candidate_id"),
        supplied=data,
    )
    force_refresh = bool(data.get("force_refresh", False))
    return generate_advisory(db, package, force_refresh=force_refresh)


@app.get("/api/candles/{symbol}")
def candles(symbol: str, interval: str = "5m", period: str = "5d", refresh: bool = False):
    try:
        df = fetch_candles(symbol, interval=interval, period=period, refresh=refresh)
        return _candles_to_json(df)
    except DataProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/quote/{symbol}")
def quote(symbol: str):
    try:
        return fetch_quote(symbol)
    except DataProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/indicators/{symbol}")
def indicators(symbol: str, interval: str = "5m", period: str = "5d", refresh: bool = False):
    try:
        df = fetch_candles(symbol, interval=interval, period=period, refresh=refresh)
        data_meta = dict(getattr(df, "attrs", {}) or {})
        cfg = config_manager.get("indicators", default={})
        enriched = apply_indicators(df, cfg)
        enriched["ema_200"] = enriched["close"].ewm(span=200, adjust=False).mean()

        candles_json = _candles_to_json(enriched)
        overlays = []
        for idx, row in enriched.iterrows():
            ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
            overlays.append(
                {
                    "time": ts,
                    "ema_fast": _json_safe(row.get("ema_fast")),
                    "ema_slow": _json_safe(row.get("ema_slow")),
                    "ema_trend": _json_safe(row.get("ema_trend")),
                    "ema_200": _json_safe(row.get("ema_200")),
                    "vwap": _json_safe(row.get("vwap")),
                    "rsi": _json_safe(row.get("rsi")),
                    "macd_line": _json_safe(row.get("macd_line")),
                    "macd_signal": _json_safe(row.get("macd_signal")),
                    "macd_hist": _json_safe(row.get("macd_hist")),
                    "bb_upper": _json_safe(row.get("bb_upper")),
                    "bb_mid": _json_safe(row.get("bb_mid")),
                    "bb_lower": _json_safe(row.get("bb_lower")),
                    "atr": _json_safe(row.get("atr")),
                    "volume_avg": _json_safe(row.get("volume_avg")),
                    "volume_spike": bool(row.get("volume_spike", False)),
                }
            )

        latest = overlays[-1] if overlays else {}
        provider_name = str(data_meta.get("provider") or data_meta.get("source") or config_manager.get("data", "candles_provider", default="yahoo"))
        source_name = str(data_meta.get("source") or provider_name)
        last_updated = data_meta.get("last_updated") or data_meta.get("timestamp")
        return {
            "symbol": normalize_symbol(symbol),
            "provider": provider_name,
            "source": source_name,
            "timestamp": last_updated,
            "last_updated": last_updated,
            "candles": candles_json,
            "indicators": overlays,
            "latest": latest,
            "line_indicators": [
                {"key": "ema_fast", "label": "EMA 9", "color": "#16c784"},
                {"key": "ema_slow", "label": "EMA 21", "color": "#f0b90b"},
                {"key": "ema_trend", "label": "EMA 50", "color": "#ef4444"},
                {"key": "ema_200", "label": "EMA 200", "color": "#a78bfa"},
                {"key": "vwap", "label": "VWAP", "color": "#60a5fa"},
            ],
            "warnings": [],
        }
    except Exception as exc:
        provider_name = config_manager.get("data", "candles_provider", default="yahoo")
        return {
            "symbol": normalize_symbol(symbol),
            "provider": provider_name,
            "source": provider_name,
            "timestamp": None,
            "last_updated": None,
            "candles": [],
            "indicators": [],
            "latest": {},
            "warnings": [str(exc)],
        }


@app.get("/api/scan")
async def get_scan(db: Session = Depends(get_db)):
    global _latest_scan_results, _last_scan_time

    if not _latest_scan_results:
        persisted = _latest_scans_from_db(db)
        if persisted:
            _latest_scan_results = persisted
    elif normalized not in _latest_scan_results:
        persisted = _latest_scans_from_db(db)
        if normalized in persisted:
            _latest_scan_results[normalized] = persisted[normalized]
            # Best effort: use freshest row timestamp.
            _last_scan_time = max(v.get("timestamp") for v in persisted.values() if v.get("timestamp"))

    if (not _latest_scan_results) or _scan_cache_stale():
        _start_scan_refresh_if_needed()

    return {
        "timestamp": _last_scan_time,
        "count": len(_latest_scan_results),
        "results": list(_latest_scan_results.values()),
        "refresh_in_progress": _scan_refresh_task is not None,
    }


@app.get("/api/scan/{symbol}")
async def get_scan_symbol(symbol: str, db: Session = Depends(get_db)):
    global _latest_scan_results
    normalized = normalize_symbol(symbol)

    # Scan requests must be read-first. A stale cache should never make the
    # request wait on Yahoo/E*TRADE/provider throttling; the refresh loop can
    # update the result asynchronously.
    if not _latest_scan_results:
        persisted = _latest_scans_from_db(db)
        if persisted:
            _latest_scan_results = persisted

    cached = _latest_scan_results.get(normalized)
    refresh_needed = _scan_cache_stale()
    if refresh_needed:
        _start_scan_refresh_if_needed()

    if cached:
        return {
            **cached,
            "cache_status": "stale_refreshing" if refresh_needed else "fresh",
            "refresh_in_progress": _scan_refresh_task is not None and not _scan_refresh_task.done(),
        }

    # There is no stored result yet. Start the normal background scan and
    # return immediately so a provider outage cannot turn this into a 504.
    _start_scan_refresh_if_needed()
    return {
        "symbol": normalized,
        "price": 0.0,
        "side": "NEUTRAL",
        "score": 0,
        "max_score": 8,
        "grade": "NO_TRADE",
        "reasons": [],
        "warnings": ["No cached scan is available yet; the background scanner is refreshing this symbol."],
        "timestamp": _last_scan_time,
        "provider": None,
        "source": "cached",
        "last_updated": None,
        "indicators": {},
        "option_ratios": None,
        "alert": False,
        "cache_status": "loading",
        "refresh_in_progress": _scan_refresh_task is not None and not _scan_refresh_task.done(),
    }


@app.post("/api/scan/run")
async def run_scan_now(db: Session = Depends(get_db)):
    _start_scan_refresh_if_needed()
    return {
        "ok": True,
        "started": True,
        "timestamp": _last_scan_time,
        "refresh_in_progress": _scan_refresh_task is not None,
    }


@app.get("/api/options/{symbol}")
def options_metadata(symbol: str):
    try:
        selection = provider_factory.get_options_provider()
        expirations, provider_name, warning = provider_factory.with_fallback(
            selection, "get_option_expirations", normalize_symbol(symbol)
        )
        payload = {
            "symbol": normalize_symbol(symbol),
            "provider": provider_name,
            "source": provider_name,
            "expirations": expirations,
            "warning": warning,
            "warnings": [warning] if warning else [],
        }
        return payload
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/options/{symbol}/ratios")
def options_ratios(symbol: str):
    expirations_to_check = int(config_manager.get("options", "expirations_to_check", default=3))
    return calculate_ratios(symbol, expirations_to_check=expirations_to_check)


@app.get("/api/options/{symbol}/contracts")
def options_contracts(symbol: str, db: Session = Depends(get_db)):
    normalized = normalize_symbol(symbol)
    options_cfg = config_manager.get("options", default={})
    chart_signal = _latest_scan_results.get(normalized)
    if chart_signal is None:
        chart_signal = _latest_scans_from_db(db).get(normalized)
    if chart_signal is None:
        chart_signal = {
            "symbol": normalized,
            "side": "NEUTRAL",
            "score": 0,
            "max_score": 8,
            "grade": "NO_TRADE",
            "reasons": [],
            "warnings": ["No stored chart signal is available yet."],
        }

    return ranked_contracts(
        normalized,
        expirations_to_check=int(options_cfg.get("expirations_to_check", 3)),
        min_volume=int(options_cfg.get("min_volume", 1)),
        max_spread_pct=float(options_cfg.get("max_spread_pct", 15)),
        min_open_interest=int(options_cfg.get("min_open_interest", 1)),
        chart_signal=chart_signal,
        options_sentiment=None,
    )


@app.post("/api/ai/trade-gate")
def ai_trade_gate(payload: dict[str, Any]):
    return validate_trade_gate(payload)


@app.get("/api/backtest/{symbol}")
def backtest_symbol(
    symbol: str,
    side: str,
    interval: str = "5m",
    period: str = "60d",
    score: int | None = None,
):
    try:
        cfg = config_manager.get("indicators", default={})
        data_cfg = config_manager.get("data", default={}) or {}
        candles_provider = str(data_cfg.get("candles_provider", "")).strip().lower()
        quotes_provider = str(data_cfg.get("quotes_provider", "")).strip().lower()
        backtest_mode = str(data_cfg.get("backtest_mode", "auto")).strip().lower()
        prefer_local_history = backtest_mode in {"local", "scan_history"} or (
            backtest_mode == "auto" and candles_provider == "etrade"
        )
        return backtest_setup(
            symbol,
            cfg,
            side=side,
            interval=interval,
            period=period,
            current_score=score,
            prefer_local_history=prefer_local_history,
        )
    except Exception as exc:
        return {
            "symbol": normalize_symbol(symbol),
            "side": side.upper(),
            "interval": interval,
            "period": period,
            "occurrences": 0,
            "wins": 0,
            "win_rate_pct": None,
            "sample_confidence": "LOW",
            "historical_edge": "UNKNOWN",
            "confidence": "LOW",
            "confidence_ok": False,
            "last_similar_setup": None,
            "sample_trades": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "warning": str(exc),
            "warnings": [str(exc)],
        }


@app.get("/api/backtest/summary/{symbol}")
def backtest_summary(symbol: str, interval: str = "5m", period: str = "60d", score: int | None = None):
    cfg = config_manager.get("indicators", default={})
    results: dict[str, Any] = {}
    warnings: list[str] = []
    for side in ["LONG", "SHORT"]:
        try:
            results[side.lower()] = backtest_setup(
                symbol,
                cfg,
                side=side,
                interval=interval,
                period=period,
                current_score=score,
                prefer_stored_candles=True,
            )
        except Exception as exc:
            warnings.append(f"{side}: {exc}")
            results[side.lower()] = {
                "symbol": normalize_symbol(symbol),
                "side": side,
                "interval": interval,
                "period": period,
                "occurrences": 0,
                "wins": 0,
                "win_rate_pct": None,
                "sample_confidence": "LOW",
                "historical_edge": "UNKNOWN",
                "confidence": "LOW",
                "confidence_ok": False,
                "warnings": [str(exc)],
            }
    return {
        "symbol": normalize_symbol(symbol),
        "interval": interval,
        "period": period,
        "source": "sqlite_preferred",
        "results": results,
        "warnings": warnings,
    }


@app.get("/api/alerts")
def get_alerts(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.query(Alert).order_by(desc(Alert.created_at)).limit(limit).all()
    return {
        "count": len(rows),
        "alerts": [
            {
                "symbol": r.symbol,
                "side": r.side,
                "score": r.score,
                "price": r.price,
                "reasons": (r.reasons or "").split(" | ") if r.reasons else [],
                "timestamp": r.created_at,
            }
            for r in rows
        ],
    }
