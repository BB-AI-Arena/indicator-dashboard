from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import and_, desc
from sqlalchemy.orm import Session

from .data_provider import DataProviderError, fetch_candles, fetch_quote, normalize_symbol
from .indicators import apply_indicators
from .models import Alert, Scan, Watchlist
from .options import calculate_ratios


def grade_from_score(score: int) -> str:
    if score < 4:
        return "NO_TRADE"
    if score <= 5:
        return "WATCH"
    if score <= 7:
        return "TRADE_CANDIDATE"
    return "HIGH_CONVICTION"


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        if value is None:
            return fallback
        if isinstance(value, float) and np.isnan(value):
            return fallback
        return float(value)
    except Exception:
        return fallback


def analyze_symbol(
    symbol: str,
    indicator_cfg: dict[str, Any],
    interval: str,
    period: str,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    candles = fetch_candles(normalized, interval=interval, period=period)
    candle_meta = dict(getattr(candles, "attrs", {}) or {})
    enriched = apply_indicators(candles, indicator_cfg)
    if len(enriched) < 3:
        raise DataProviderError(f"Not enough candle data for {normalized}")

    latest = enriched.iloc[-1]
    prev = enriched.iloc[-2]

    long_score = 0
    short_score = 0
    long_reasons: list[str] = []
    short_reasons: list[str] = []
    warnings: list[str] = []

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

    if np.isnan(vwap):
        warnings.append("VWAP unavailable")
    elif close > vwap:
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

    max_score = 8
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
        "symbol": normalized,
        "price": round(close, 4),
        "side": side,
        "score": int(score),
        "max_score": max_score,
        "grade": grade_from_score(int(score)),
        "reasons": reasons,
        "warnings": warnings,
        "timestamp": datetime.utcnow().isoformat(),
        "provider": candle_meta.get("provider"),
        "source": candle_meta.get("source") or candle_meta.get("provider"),
        "last_updated": candle_meta.get("last_updated") or candle_meta.get("timestamp"),
        "indicators": {
            "close": close,
            "vwap": vwap,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "ema_trend": ema_trend,
            "rsi": rsi,
            "macd_hist": macd_hist,
            "macd_hist_prev": prev_macd_hist,
            "bb_mid": bb_mid,
            "volume": volume,
            "volume_avg": volume_avg,
            "volume_spike": bool(volume > volume_avg),
        },
    }


def save_scan(db: Session, scan_result: dict[str, Any]) -> Scan:
    record = Scan(
        symbol=scan_result["symbol"],
        side=scan_result["side"],
        score=scan_result["score"],
        max_score=scan_result["max_score"],
        grade=scan_result["grade"],
        price=scan_result["price"],
        reasons=" | ".join(scan_result.get("reasons", [])),
        warnings=" | ".join(scan_result.get("warnings", [])),
        created_at=scan_result["timestamp"],
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def maybe_save_alert(db: Session, scan_result: dict[str, Any], min_score: int, cooldown_minutes: int) -> bool:
    if scan_result["score"] < min_score:
        return False

    threshold = datetime.utcnow() - timedelta(minutes=cooldown_minutes)
    threshold_iso = threshold.isoformat()
    prior = (
        db.query(Alert)
        .filter(
            and_(
                Alert.symbol == scan_result["symbol"],
                Alert.created_at >= threshold_iso,
                Alert.side == scan_result["side"],
            )
        )
        .order_by(desc(Alert.created_at))
        .first()
    )

    if prior:
        return False

    alert = Alert(
        symbol=scan_result["symbol"],
        side=scan_result["side"],
        score=scan_result["score"],
        price=scan_result["price"],
        reasons=" | ".join(scan_result.get("reasons", [])),
        created_at=scan_result["timestamp"],
    )
    db.add(alert)
    db.commit()
    return True


def ensure_watchlist_seeded(db: Session, symbols: list[str]) -> None:
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if not normalized:
            continue
        exists = db.query(Watchlist).filter(Watchlist.symbol == normalized).first()
        if not exists:
            db.add(
                Watchlist(
                    symbol=normalized,
                    source="config",
                    active=True,
                    added_at=datetime.utcnow().isoformat(),
                )
            )
    db.commit()


def get_watchlist_symbols(db: Session) -> list[str]:
    rows = db.query(Watchlist).filter(Watchlist.active.is_(True)).all()
    return [r.symbol for r in rows]


def run_scan_for_symbols(
    db: Session,
    symbols: list[str],
    scan_cfg: dict[str, Any],
    indicator_cfg: dict[str, Any],
    options_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    interval = scan_cfg.get("interval", "5m")
    period = scan_cfg.get("period", "5d")
    min_score_to_alert = int(scan_cfg.get("min_score_to_alert", 6))
    cooldown = int(scan_cfg.get("alert_cooldown_minutes", 30))
    options_enabled = bool(options_cfg.get("enabled", True))
    expirations_to_check = int(options_cfg.get("expirations_to_check", 3))

    results: list[dict[str, Any]] = []

    for symbol in symbols:
        try:
            result = analyze_symbol(symbol, indicator_cfg, interval, period)
            if options_enabled:
                try:
                    ratios = calculate_ratios(symbol, expirations_to_check=expirations_to_check)
                    agg = dict(ratios.get("aggregate") or {})
                    agg["source"] = ratios.get("source")
                    agg["provider"] = ratios.get("provider")
                    agg["quote_type"] = ratios.get("quote_type")
                    agg["warning"] = ratios.get("warning")
                    result["option_ratios"] = agg
                except Exception as exc:
                    result.setdefault("warnings", []).append(f"Options ratios unavailable: {exc}")
            save_scan(db, result)
            alerted = maybe_save_alert(db, result, min_score_to_alert, cooldown)
            result["alert"] = alerted
            results.append(result)
        except Exception as exc:
            normalized = normalize_symbol(symbol)
            warnings = [str(exc)]
            price = 0.0
            option_ratios = None
            provider = None
            source = None
            last_updated = None

            try:
                quote = fetch_quote(normalized)
                price = float(quote.get("price") or 0.0)
                provider = quote.get("provider") or quote.get("source")
                source = quote.get("source") or provider
                last_updated = quote.get("timestamp")
                if provider:
                    warnings.append(f"Quote fallback from {provider}")
                if quote.get("warning"):
                    warnings.append(str(quote.get("warning")))
            except Exception as quote_exc:
                warnings.append(f"Quote unavailable: {quote_exc}")

            if options_enabled:
                try:
                    ratios = calculate_ratios(normalized, expirations_to_check=expirations_to_check)
                    option_ratios = ratios.get("aggregate")
                    if option_ratios is not None:
                        option_ratios["source"] = ratios.get("source")
                        option_ratios["provider"] = ratios.get("provider")
                    if ratios.get("warning"):
                        warnings.append(str(ratios.get("warning")))
                except Exception as ratio_exc:
                    warnings.append(f"Options ratios unavailable: {ratio_exc}")

            results.append(
                {
                    "symbol": normalized,
                    "price": round(float(price), 4),
                    "side": "NEUTRAL",
                    "score": 0,
                    "max_score": 8,
                    "grade": "NO_TRADE",
                    "reasons": [],
                    "warnings": warnings,
                    "timestamp": datetime.utcnow().isoformat(),
                    "provider": provider,
                    "source": source,
                    "last_updated": last_updated,
                    "indicators": {},
                    "option_ratios": option_ratios,
                    "alert": False,
                }
            )
        # Avoid hammering yfinance for large watchlists.
        time.sleep(0.35)

    return results
