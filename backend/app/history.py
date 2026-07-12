from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import asc, desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import config_manager
from .db import SessionLocal
from .models import BackfillChunk, BackfillRun, Candle, ProviderErrorLog, Scan, Watchlist


CHUNK_STATUSES = {"PENDING", "RUNNING", "COMPLETE", "FAILED", "SKIPPED"}
INCOMPLETE_RUN_STATUSES = {"PENDING", "RUNNING", "FAILED"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def period_to_timedelta(period: str) -> timedelta:
    text = str(period or "5d").strip().lower()
    units = [
        ("mo", 30),
        ("wk", 7),
        ("y", 365),
        ("d", 1),
    ]
    for suffix, days in units:
        if text.endswith(suffix):
            try:
                return timedelta(days=max(1, int(text[: -len(suffix)]) * days))
            except Exception:
                return timedelta(days=5)
    if text.endswith("h"):
        try:
            return timedelta(hours=max(1, int(text[:-1])))
        except Exception:
            return timedelta(days=5)
    if text.endswith("m"):
        try:
            return timedelta(minutes=max(1, int(text[:-1])))
        except Exception:
            return timedelta(days=5)
    return timedelta(days=5)


def interval_seconds(interval: str) -> int:
    text = str(interval or "5m").strip().lower()
    if text.endswith("m"):
        return max(60, int(float(text[:-1]) * 60))
    if text.endswith("h"):
        return max(3600, int(float(text[:-1]) * 3600))
    if text.endswith("d"):
        return 86400
    return 300


def is_intraday_interval(interval: str) -> bool:
    return interval_seconds(interval) < 86400


def candle_rows_to_dataframe(rows: list[Candle]) -> pd.DataFrame:
    payload = [
        {
            "time": int(row.timestamp),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume or 0.0),
        }
        for row in rows
    ]
    if not payload:
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df.attrs.update({"provider": None, "source": "sqlite", "timestamp": None, "last_updated": None})
        return df
    df = pd.DataFrame(payload)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    provider = next((row.provider for row in reversed(rows) if getattr(row, "provider", None)), None)
    last_updated = next((row.updated_at for row in reversed(rows) if getattr(row, "updated_at", None)), None)
    df.attrs.update(
        {
            "provider": provider or "sqlite",
            "source": provider or "sqlite",
            "timestamp": last_updated or None,
            "last_updated": last_updated or None,
        }
    )
    return df[["open", "high", "low", "close", "volume"]]


def get_candles_from_sql(
    symbol: str,
    interval: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    period: str | None = None,
    db: Session | None = None,
) -> pd.DataFrame:
    close_db = db is None
    session = db or SessionLocal()
    try:
        normalized = normalize_symbol(symbol)
        end_dt = end or now_utc()
        start_dt = start or (end_dt - period_to_timedelta(period or "5d"))
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())
        rows = (
            session.query(Candle)
            .filter(Candle.symbol == normalized)
            .filter(Candle.interval == interval)
            .filter(Candle.timestamp >= start_ts)
            .filter(Candle.timestamp <= end_ts)
            .order_by(asc(Candle.timestamp))
            .all()
        )
        return candle_rows_to_dataframe(rows)
    finally:
        if close_db:
            session.close()


def stored_range(symbol: str, interval: str, start: datetime, end: datetime, db: Session) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    row = (
        db.query(
            func.min(Candle.timestamp),
            func.max(Candle.timestamp),
            func.count(Candle.id),
        )
        .filter(Candle.symbol == normalized)
        .filter(Candle.interval == interval)
        .filter(Candle.timestamp >= start_ts)
        .filter(Candle.timestamp <= end_ts)
        .first()
    )
    min_ts, max_ts, count = row if row else (None, None, 0)
    return {"min_ts": min_ts, "max_ts": max_ts, "count": int(count or 0)}


def range_coverage_complete(symbol: str, interval: str, start: datetime, end: datetime, db: Session) -> bool:
    coverage = stored_range(symbol, interval, start, end, db)
    if not coverage["count"] or coverage["min_ts"] is None or coverage["max_ts"] is None:
        return False
    grace = 86400 if is_intraday_interval(interval) else 86400 * 3
    return coverage["min_ts"] <= int(start.timestamp()) + grace and coverage["max_ts"] >= int(end.timestamp()) - grace


def upsert_candles(
    symbol: str,
    interval: str,
    candles: list[dict[str, Any]],
    provider: str | None,
    db: Session | None = None,
) -> tuple[int, int]:
    close_db = db is None
    session = db or SessionLocal()
    inserted = 0
    updated = 0
    normalized = normalize_symbol(symbol)
    timestamp = now_iso()
    try:
        for row in candles or []:
            ts = int(row.get("time", 0) or 0)
            if ts <= 0:
                continue
            values = {
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "close": float(row.get("close", 0) or 0),
                "volume": float(row.get("volume", 0) or 0),
            }
            existing = (
                session.query(Candle)
                .filter(Candle.symbol == normalized)
                .filter(Candle.interval == interval)
                .filter(Candle.timestamp == ts)
                .first()
            )
            if existing:
                changed = False
                for key, value in values.items():
                    if float(getattr(existing, key) or 0) != value:
                        setattr(existing, key, value)
                        changed = True
                if provider and existing.provider != provider:
                    existing.provider = provider
                    changed = True
                if changed:
                    existing.updated_at = timestamp
                    updated += 1
                continue

            session.add(
                Candle(
                    symbol=normalized,
                    interval=interval,
                    timestamp=ts,
                    provider=provider,
                    created_at=timestamp,
                    updated_at=timestamp,
                    **values,
                )
            )
            inserted += 1
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return upsert_candles(normalized, interval, candles, provider, session)
        return inserted, updated
    finally:
        if close_db:
            session.close()


def requested_symbols(payload: dict[str, Any] | None = None) -> list[str]:
    payload = payload or {}
    raw = payload.get("symbols")
    all_symbols = bool(payload.get("all_symbols")) or str(raw or "").strip().lower() in {"all", "*", "__all__"}
    if isinstance(raw, str) and not all_symbols:
        symbols = [raw]
    elif isinstance(raw, list) and raw:
        symbols = raw
    else:
        db = SessionLocal()
        try:
            symbols = [row.symbol for row in db.query(Watchlist).filter(Watchlist.active.is_(True)).all()]
        finally:
            db.close()
        if not symbols:
            symbols = list(config_manager.get("scan", "symbols", default=[]) or [])
    configured_limit = config_manager.get("history", "max_symbols_per_run", default=25)
    requested_limit = payload.get("max_symbols")
    try:
        limit = int(requested_limit if requested_limit is not None else configured_limit)
    except Exception:
        limit = 25
    normalized_symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
    if limit > 0 and not all_symbols:
        return normalized_symbols[:limit]
    if limit > 0 and all_symbols and requested_limit is not None:
        return normalized_symbols[:limit]
    return normalized_symbols


def requested_intervals(payload: dict[str, Any] | None = None) -> list[str]:
    payload = payload or {}
    raw = payload.get("intervals")
    intervals = raw if isinstance(raw, list) and raw else config_manager.get("history", "intervals", default=["5m", "15m", "1d"])
    allow_1m = bool(payload.get("include_1m", False))
    return [str(i).strip() for i in intervals if str(i).strip() and (str(i).strip() != "1m" or allow_1m)]


def backfill_period(payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    return str(payload.get("period") or config_manager.get("history", "default_period", default="1y"))


def effective_backfill_start(interval: str, period: str, end: datetime) -> tuple[datetime, str | None]:
    start = end - period_to_timedelta(period)
    if is_intraday_interval(interval):
        max_days = int(config_manager.get("history", "intraday_initial_days", default=90) or 90)
        capped = end - timedelta(days=max_days)
        if start < capped:
            return capped, (
                f"Provider intraday history may be limited. Starting with {max_days} days for {interval}; "
                "stored available data will continue building over time."
            )
    return start, None


def chunk_ranges(interval: str, period: str) -> tuple[list[tuple[datetime, datetime]], list[str]]:
    end = now_utc()
    start, warning = effective_backfill_start(interval, period, end)
    chunk_days = int(
        config_manager.get(
            "history",
            "backfill_chunk_days_intraday" if is_intraday_interval(interval) else "backfill_chunk_days_daily",
            default=7 if is_intraday_interval(interval) else 365,
        )
        or (7 if is_intraday_interval(interval) else 365)
    )
    ranges: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days))
        ranges.append((cursor, chunk_end))
        cursor = chunk_end
    warnings = [warning] if warning else []
    return ranges, warnings


def create_or_resume_backfill_run(payload: dict[str, Any] | None = None) -> BackfillRun:
    payload = payload or {}
    symbols = requested_symbols(payload)
    intervals = requested_intervals(payload)
    period = backfill_period(payload)
    resume = bool(config_manager.get("history", "resume_incomplete_backfills", default=True))

    db = SessionLocal()
    try:
        active = (
            db.query(BackfillRun)
            .filter(BackfillRun.status == "RUNNING")
            .order_by(desc(BackfillRun.started_at))
            .first()
        )
        if active:
            return active

        run = None
        if resume:
            run = (
                db.query(BackfillRun)
                .filter(BackfillRun.status.in_(["PENDING", "FAILED"]))
                .order_by(desc(BackfillRun.started_at))
                .first()
            )
        if run:
            run.status = "RUNNING"
            run.finished_at = None
            run.message = "Resuming incomplete backfill"
            db.commit()
            db.refresh(run)
            return run

        warnings: list[str] = []
        run = BackfillRun(
            status="RUNNING",
            symbols=json.dumps(symbols),
            intervals=json.dumps(intervals),
            period=period,
            started_at=now_iso(),
            chunks_total=0,
            chunks_completed=0,
            chunks_failed=0,
            rows_inserted=0,
            rows_updated=0,
            error_count=0,
            message="Creating chunks",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        chunks: list[BackfillChunk] = []
        for symbol in symbols:
            for interval in intervals:
                ranges, interval_warnings = chunk_ranges(interval, period)
                warnings.extend(interval_warnings)
                for start, end in ranges:
                    chunks.append(
                        BackfillChunk(
                            run_id=run.id,
                            symbol=symbol,
                            interval=interval,
                            start_timestamp=start.isoformat(),
                            end_timestamp=end.isoformat(),
                            status="PENDING",
                            created_at=now_iso(),
                        )
                    )
        for chunk in chunks:
            db.add(chunk)
        run.chunks_total = len(chunks)
        run.message = " | ".join(sorted(set(warnings))) if warnings else "Backfill queued"
        db.commit()
        db.refresh(run)
        return run
    finally:
        db.close()


def latest_provider_error(db: Session | None = None) -> ProviderErrorLog | None:
    close_db = db is None
    session = db or SessionLocal()
    try:
        return session.query(ProviderErrorLog).order_by(desc(ProviderErrorLog.created_at)).first()
    finally:
        if close_db:
            session.close()


def db_status() -> dict[str, Any]:
    db = SessionLocal()
    try:
        interval_rows = (
            db.query(Candle.interval, func.count(Candle.id), func.count(func.distinct(Candle.symbol)))
            .group_by(Candle.interval)
            .all()
        )
        return {
            "candles": db.query(Candle).count(),
            "candle_symbols": db.query(Candle.symbol).distinct().count(),
            "active_watchlist_symbols": db.query(Watchlist).filter(Watchlist.active.is_(True)).count(),
            "candle_intervals": [
                {"interval": interval, "rows": int(rows or 0), "symbols": int(symbols or 0)}
                for interval, rows, symbols in interval_rows
            ],
            "scans": db.query(Scan).count(),
            "provider_errors": db.query(ProviderErrorLog).count(),
            "backfill_runs": db.query(BackfillRun).count(),
            "backfill_chunks": db.query(BackfillChunk).count(),
        }
    finally:
        db.close()
