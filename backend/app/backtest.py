from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .data_provider import DataProviderError, fetch_candles, normalize_symbol
from .db import SessionLocal
from .indicators import apply_indicators
from .models import Scan

MIN_SAMPLE_CONFIDENCE_OCCURRENCES = 20
ENOUGH_SAMPLE_OCCURRENCES = 50
MIN_HISTORICAL_WIN_RATE_PCT = 52.0


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        if value is None:
            return fallback
        if isinstance(value, float) and np.isnan(value):
            return fallback
        return float(value)
    except Exception:
        return fallback


def _grade_from_score(score: int) -> str:
    if score < 4:
        return "NO_TRADE"
    if score <= 5:
        return "WATCH"
    if score <= 7:
        return "TRADE_CANDIDATE"
    return "HIGH_CONVICTION"


def _sample_confidence(total: int) -> str:
    if total < MIN_SAMPLE_CONFIDENCE_OCCURRENCES:
        return "LOW"
    if total < ENOUGH_SAMPLE_OCCURRENCES:
        return "MEDIUM"
    return "ENOUGH"


def _historical_edge(win_rate: float | None) -> str:
    if win_rate is None:
        return "UNKNOWN"
    if win_rate < MIN_HISTORICAL_WIN_RATE_PCT:
        return "WEAK"
    if win_rate <= 56:
        return "SLIGHT"
    if win_rate <= 60:
        return "MODERATE"
    return "STRONG"


def _combined_confidence(sample_confidence: str, historical_edge: str) -> str:
    if sample_confidence == "LOW" or historical_edge in {"UNKNOWN", "WEAK"}:
        return "LOW"
    if sample_confidence == "ENOUGH" and historical_edge == "STRONG":
        return "HIGH"
    return "MEDIUM"


def _confidence_ok(sample_confidence: str, historical_edge: str) -> bool:
    return sample_confidence in {"MEDIUM", "ENOUGH"} and historical_edge in {"SLIGHT", "MODERATE", "STRONG"}


def _score_row(latest: pd.Series, prev: pd.Series) -> dict[str, Any]:
    long_score = 0
    short_score = 0
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    close = _safe_float(latest.get("close"))
    high = _safe_float(latest.get("high"))
    low = _safe_float(latest.get("low"))
    prev_high = _safe_float(prev.get("high"))
    prev_low = _safe_float(prev.get("low"))
    vwap = _safe_float(latest.get("vwap"), fallback=np.nan)
    ema_fast = _safe_float(latest.get("ema_fast"), fallback=np.nan)
    ema_slow = _safe_float(latest.get("ema_slow"), fallback=np.nan)
    ema_trend = _safe_float(latest.get("ema_trend"), fallback=np.nan)
    rsi = _safe_float(latest.get("rsi"), fallback=np.nan)
    macd_hist = _safe_float(latest.get("macd_hist"), fallback=np.nan)
    prev_macd_hist = _safe_float(prev.get("macd_hist"), fallback=np.nan)
    volume = _safe_float(latest.get("volume"))
    volume_avg = _safe_float(latest.get("volume_avg"), fallback=np.nan)
    bb_mid = _safe_float(latest.get("bb_mid"), fallback=np.nan)

    if not np.isnan(vwap):
        if close > vwap:
            long_score += 1
            long_reasons.append("Close above VWAP")
        else:
            short_score += 1
            short_reasons.append("Close below VWAP")

    if ema_fast > ema_slow:
        long_score += 1
        long_reasons.append("EMA fast above EMA slow")
    elif ema_fast < ema_slow:
        short_score += 1
        short_reasons.append("EMA fast below EMA slow")

    if ema_slow > ema_trend:
        long_score += 1
        long_reasons.append("EMA slow above EMA trend")
    elif ema_slow < ema_trend:
        short_score += 1
        short_reasons.append("EMA slow below EMA trend")

    if macd_hist > 0 and macd_hist > prev_macd_hist:
        long_score += 1
        long_reasons.append("MACD histogram positive and rising")
    if macd_hist < 0 and macd_hist < prev_macd_hist:
        short_score += 1
        short_reasons.append("MACD histogram negative and falling")

    if 45 <= rsi <= 68:
        long_score += 1
        long_reasons.append("RSI in long zone (45-68)")
    if 32 <= rsi <= 55:
        short_score += 1
        short_reasons.append("RSI in short zone (32-55)")

    if volume > volume_avg:
        long_score += 1
        short_score += 1
        long_reasons.append("Volume above average")
        short_reasons.append("Volume above average")

    if close > bb_mid:
        long_score += 1
        long_reasons.append("Close above Bollinger middle")
    elif close < bb_mid:
        short_score += 1
        short_reasons.append("Close below Bollinger middle")

    if high > prev_high:
        long_score += 1
        long_reasons.append("Current high breaks prior high")
    if low < prev_low:
        short_score += 1
        short_reasons.append("Current low breaks prior low")

    if long_score > short_score:
        side = "LONG"
        score = long_score
        reasons = long_reasons
    elif short_score > long_score:
        side = "SHORT"
        score = short_score
        reasons = short_reasons
    else:
        side = "NEUTRAL"
        score = max(long_score, short_score)
        reasons = ["Long and short scores are tied"]

    return {
        "side": side,
        "score": int(score),
        "max_score": 8,
        "grade": _grade_from_score(int(score)),
        "reasons": reasons,
    }


def _parse_iso_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _backtest_from_scan_history(
    symbol: str,
    target_side: str,
    min_score: int,
    interval: str,
    period: str,
    upstream_warning: str,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        rows = db.query(Scan).filter(Scan.symbol == symbol).order_by(Scan.created_at.asc()).all()
    finally:
        db.close()

    if not rows:
        return {
            "symbol": symbol,
            "side": target_side,
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
            "source": "scan_history_approx",
            "warning": f"{upstream_warning} | No scan history available for fallback",
            "warnings": [upstream_warning, "No scan history available for fallback"],
        }

    daily_rows: dict[str, list[Scan]] = {}
    for row in rows:
        ts = _parse_iso_ts(row.created_at)
        if ts is None:
            continue
        day_key = ts.date().isoformat()
        daily_rows.setdefault(day_key, []).append(row)

    trades: list[dict[str, Any]] = []
    latest_day_key = max(daily_rows.keys()) if daily_rows else None
    for day in sorted(daily_rows.keys()):
        if latest_day_key and day == latest_day_key:
            # Exclude current day to avoid reporting same-session setups as historical precedent.
            continue
        day_rows = sorted(
            daily_rows[day],
            key=lambda r: _parse_iso_ts(r.created_at) or datetime.min.replace(tzinfo=timezone.utc),
        )
        if len(day_rows) < 2:
            continue
        entry_row = next(
            (
                r
                for r in day_rows
                if str(r.side).upper() == target_side and int(r.score) >= int(min_score)
            ),
            None,
        )
        if entry_row is None:
            continue
        exit_row = day_rows[-1]
        if entry_row.id == exit_row.id:
            continue
        entry_price = _safe_float(getattr(entry_row, "price", 0.0))
        exit_price = _safe_float(getattr(exit_row, "price", 0.0))
        if entry_price <= 0 or exit_price <= 0:
            continue

        raw_ret = ((exit_price - entry_price) / entry_price) * 100.0
        signed_ret = raw_ret if target_side == "LONG" else -raw_ret
        trades.append(
            {
                "setup_time": entry_row.created_at,
                "entry_time": entry_row.created_at,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "day_return_pct": round(signed_ret, 4),
                "profitable": signed_ret > 0,
                "score": int(entry_row.score),
                "grade": entry_row.grade,
            }
        )

    latest_side_scan = next(
        (r for r in reversed(rows) if str(r.side).upper() == target_side),
        None,
    )
    current_score = int(getattr(latest_side_scan, "score", min_score))
    current_grade = str(getattr(latest_side_scan, "grade", _grade_from_score(current_score)))

    total = len(trades)
    wins = sum(1 for t in trades if t["profitable"])
    win_rate = round((wins / total) * 100, 2) if total else None
    sample_confidence = _sample_confidence(total)
    historical_edge = _historical_edge(win_rate)
    confidence = _combined_confidence(sample_confidence, historical_edge)
    confidence_ok = _confidence_ok(sample_confidence, historical_edge)
    last = trades[-1] if trades else None

    warnings = [upstream_warning, "Using scan-history fallback"]
    if sample_confidence == "LOW":
        warnings.append(
            f"Insufficient sample size for confidence ({total}/{MIN_SAMPLE_CONFIDENCE_OCCURRENCES} sessions)"
        )
    if historical_edge in {"UNKNOWN", "WEAK"}:
        warnings.append("Historical win rate is below the 52% minimum edge threshold")

    return {
        "symbol": symbol,
        "side": target_side,
        "interval": interval,
        "period": period,
        "current_setup": {
            "score": current_score,
            "grade": current_grade,
            "reasons": [
                "Based on stored scan history fallback due to candle provider limits"
            ],
        },
        "occurrences": total,
        "wins": wins,
        "win_rate_pct": win_rate,
        "sample_confidence": sample_confidence,
        "historical_edge": historical_edge,
        "confidence": confidence,
        "confidence_ok": confidence_ok,
        "last_similar_setup": last,
        "sample_trades": trades[-15:],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "scan_history_approx",
        "warning": " | ".join(warnings),
        "warnings": warnings,
    }


def backtest_setup(
    symbol: str,
    indicator_cfg: dict[str, Any],
    side: str,
    interval: str = "5m",
    period: str = "60d",
    current_score: int | None = None,
    prefer_local_history: bool = False,
    prefer_stored_candles: bool = True,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    target_side = side.strip().upper()
    if target_side not in {"LONG", "SHORT"}:
        raise DataProviderError("Backtest side must be LONG or SHORT")

    threshold = max(4, int(current_score or 4))
    if prefer_local_history:
        return _backtest_from_scan_history(
            symbol=normalized,
            target_side=target_side,
            min_score=threshold,
            interval=interval,
            period=period,
            upstream_warning=(
                "Configured to use local scan-history backtest "
                "(E*TRADE does not provide historical candle bars in this integration)"
            ),
        )

    try:
        candles = fetch_candles(
            normalized,
            interval=interval,
            period=period,
            prefer_stored=prefer_stored_candles,
            historical=True,
        )
        enriched = apply_indicators(candles, indicator_cfg)
        if len(enriched) < 30:
            raise DataProviderError(f"Not enough data to backtest {normalized}")
    except Exception as exc:
        return _backtest_from_scan_history(
            symbol=normalized,
            target_side=target_side,
            min_score=threshold,
            interval=interval,
            period=period,
            upstream_warning=str(exc),
        )

    # Current setup profile from latest available bars.
    current = _score_row(enriched.iloc[-1], enriched.iloc[-2])
    setup_score = int(current["score"])
    if current_score is not None:
        setup_score = max(setup_score, int(current_score))
    current_day = pd.Timestamp(enriched.index[-1]).date()

    trades: list[dict[str, Any]] = []
    for i in range(2, len(enriched) - 1):
        setup = _score_row(enriched.iloc[i], enriched.iloc[i - 1])
        if setup["side"] != target_side:
            continue
        if int(setup["score"]) < max(4, setup_score):
            continue

        entry_idx = i + 1
        entry_row = enriched.iloc[entry_idx]
        entry_ts = enriched.index[entry_idx]
        entry_day = pd.Timestamp(entry_ts).date()
        if entry_day == current_day:
            # Exclude current day from backtest samples; keep it as setup-under-evaluation only.
            continue

        day_slice = enriched[pd.Index(enriched.index).date == entry_day]
        if day_slice.empty:
            continue
        exit_row = day_slice.iloc[-1]

        entry = _safe_float(entry_row.get("open"))
        exit_ = _safe_float(exit_row.get("close"))
        if entry <= 0:
            continue

        raw_ret = ((exit_ - entry) / entry) * 100.0
        signed_ret = raw_ret if target_side == "LONG" else -raw_ret
        trades.append(
            {
                "setup_time": pd.Timestamp(enriched.index[i]).isoformat(),
                "entry_time": pd.Timestamp(entry_ts).isoformat(),
                "entry_price": round(entry, 4),
                "exit_price": round(exit_, 4),
                "day_return_pct": round(signed_ret, 4),
                "profitable": signed_ret > 0,
                "score": int(setup["score"]),
                "grade": setup["grade"],
            }
        )

    total = len(trades)
    wins = sum(1 for t in trades if t["profitable"])
    win_rate = round((wins / total) * 100, 2) if total else None
    sample_confidence = _sample_confidence(total)
    historical_edge = _historical_edge(win_rate)
    confidence = _combined_confidence(sample_confidence, historical_edge)
    confidence_ok = _confidence_ok(sample_confidence, historical_edge)

    last = trades[-1] if total else None
    warnings: list[str] = []
    if sample_confidence == "LOW":
        warnings.append(
            f"Insufficient sample size for confidence ({total}/{MIN_SAMPLE_CONFIDENCE_OCCURRENCES} sessions)"
        )
    if historical_edge in {"UNKNOWN", "WEAK"}:
        warnings.append("Historical win rate is below the 52% minimum edge threshold")

    return {
        "symbol": normalized,
        "side": target_side,
        "interval": interval,
        "period": period,
        "current_setup": {
            "score": setup_score,
            "grade": current["grade"],
            "reasons": current.get("reasons", []),
        },
        "occurrences": total,
        "wins": wins,
        "win_rate_pct": win_rate,
        "sample_confidence": sample_confidence,
        "historical_edge": historical_edge,
        "confidence": confidence,
        "confidence_ok": confidence_ok,
        "last_similar_setup": last,
        "sample_trades": trades[-15:],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "historical_candles",
        "warning": " | ".join(warnings) if warnings else None,
        "warnings": warnings,
    }
