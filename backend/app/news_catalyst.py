from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .cache_policy import market_aware_ttl
from .config import config_manager
from .data_provider import fetch_candles, normalize_symbol
from .earnings_calendar import upcoming_earnings_feed
from .history import get_candles_from_sql
from .market_session import get_market_session
from .models import NewsCatalystSnapshot
from .news_feeds import market_news_feed


EASTERN = ZoneInfo("America/New_York")
_LOCK = threading.Lock()
_MEM_CACHE: dict[str, dict[str, Any]] = {}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        n = float(value)
        if math.isnan(n) or math.isinf(n):
            return default
        return n
    except Exception:
        return default


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = _safe_text(value)
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_et(value: Any) -> datetime | None:
    parsed = _parse_timestamp(value)
    if not parsed:
        return None
    return parsed.astimezone(EASTERN)


def _market_days_around(reference: datetime, days_before: int, days_after: int) -> tuple[datetime, datetime]:
    return reference - timedelta(days=days_before), reference + timedelta(days=days_after)


def _normalize_headline(value: str | None) -> str:
    text = _safe_text(value).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9\s$-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180]


def _event_category(title: str, summary: str, source: str | None = None) -> str:
    text = f"{title} {summary}".lower()
    if "earnings" in text or "eps" in text or "guidance" in text or "revenue" in text:
        return "earnings"
    if any(marker in text for marker in ("upgrade", "downgrade", "price target", "initiated", "rating")):
        return "analyst upgrade or downgrade"
    if any(marker in text for marker in ("lawsuit", "sue", "regulatory", "sec ", "fda", "court", "investigation")):
        return "regulatory action"
    if any(marker in text for marker in ("product", "launch", "release", "rollout")):
        return "product launch"
    if any(marker in text for marker in ("partnership", "deal", "agreement", "contract win", "contract loss")):
        return "partnership"
    if any(marker in text for marker in ("acquisition", "acquire", "divest", "spinoff")):
        return "acquisition or divestiture"
    if any(marker in text for marker in ("insider", "ceo", "cfo", "management change")):
        return "management change"
    if any(marker in text for marker in ("cyber", "breach", "ransomware", "security")):
        return "cybersecurity incident"
    if any(marker in text for marker in ("layoff", "restructuring", "cost cut")):
        return "layoffs or restructuring"
    if any(marker in text for marker in ("share repurchase", "buyback", "dividend", "capital raise")):
        return "capital raise"
    if source and "earnings" in source.lower():
        return "earnings"
    return "other material catalyst"


def _headline_sentiment(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    positive = (
        "beat", "beats", "raised", "raise", "upgraded", "upgrade", "surge", "record", "approval",
        "approved", "partnership", "win", "wins", "buyback", "dividend", "repurchase", "strong",
    )
    negative = (
        "miss", "misses", "lowered", "cut", "downgraded", "downgrade", "lawsuit", "investigation",
        "delay", "slump", "weak", "recall", "loss", "fraud", "probe", "restructuring",
    )
    pos_hits = sum(1 for marker in positive if marker in text)
    neg_hits = sum(1 for marker in negative if marker in text)
    if pos_hits > neg_hits:
        return "positive"
    if neg_hits > pos_hits:
        return "negative"
    return "neutral"


def _clean_event_text(title: str, summary: str, symbol: str) -> str:
    text = f"{title} {summary}".strip()
    if symbol:
        text = re.sub(rf"\b{re.escape(symbol)}\b", symbol, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _factor_key(symbol: str, context_type: str, entry_ts: str | None, exit_ts: str | None) -> str:
    raw = json.dumps(
        {
            "symbol": normalize_symbol(symbol),
            "context_type": context_type,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _records_to_frame(indicator_data: dict[str, Any] | None) -> pd.DataFrame:
    if not isinstance(indicator_data, dict):
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    candles = indicator_data.get("candles") or []
    indicators = indicator_data.get("indicators") or []
    indicator_by_time = {}
    for row in indicators:
        ts = _safe_int(row.get("time"))
        if ts is not None:
            indicator_by_time[ts] = row
    rows: list[dict[str, Any]] = []
    for candle in candles:
        ts = _safe_int(candle.get("time"))
        o = _safe_float(candle.get("open"))
        h = _safe_float(candle.get("high"))
        l = _safe_float(candle.get("low"))
        c = _safe_float(candle.get("close"))
        if ts is None or o is None or h is None or l is None or c is None:
            continue
        row = {
            "time": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": _safe_float(candle.get("volume"), 0.0) or 0.0,
        }
        row.update({k: v for k, v in (indicator_by_time.get(ts) or {}).items() if k != "time"})
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    if "vwap" not in frame.columns:
        frame["vwap"] = (frame["close"] * frame["volume"]).cumsum() / frame["volume"].replace(0, pd.NA).cumsum()
        frame["vwap"] = frame["vwap"].fillna(method="ffill")
    return frame


def _nearest_before(frame: pd.DataFrame, ts: int) -> dict[str, Any] | None:
    rows = frame[frame["time"] <= ts]
    if rows.empty:
        return None
    return rows.iloc[-1].to_dict()


def _nearest_after(frame: pd.DataFrame, ts: int) -> dict[str, Any] | None:
    rows = frame[frame["time"] > ts]
    if rows.empty:
        return None
    return rows.iloc[0].to_dict()


def _value_at_or_before(frame: pd.DataFrame, ts: int, column: str) -> float | None:
    row = _nearest_before(frame, ts)
    return _safe_float(row.get(column)) if row else None


def _reaction_windows(frame: pd.DataFrame, event_ts: int) -> dict[str, Any]:
    if frame.empty:
        return {
            "data_status": "unavailable",
            "reason": "No candle data available.",
        }
    pre_row = _nearest_before(frame, event_ts)
    post_rows = frame[frame["time"] >= event_ts].copy()
    if pre_row is None or post_rows.empty:
        return {
            "data_status": "unavailable",
            "reason": "No candles surrounding the event timestamp.",
        }

    pre_price = _safe_float(pre_row.get("close"))
    first_after = post_rows.iloc[0].to_dict()
    first_price = _safe_float(first_after.get("open"), _safe_float(first_after.get("close")))
    if pre_price is None or first_price is None or pre_price <= 0:
        return {
            "data_status": "unavailable",
            "reason": "Insufficient price history for reaction analysis.",
        }

    def _return_at(minutes: int) -> float | None:
        target = event_ts + (minutes * 60)
        row = _nearest_before(frame, target)
        price = _safe_float(row.get("close")) if row else None
        if price is None:
            return None
        return round(((price - pre_price) / pre_price) * 100.0, 2)

    after_window = frame[frame["time"] >= event_ts].copy()
    after_window["change"] = after_window["close"] - pre_price
    max_favorable = round(float(after_window["change"].max()), 4) if not after_window.empty else None
    max_adverse = round(float(after_window["change"].min()), 4) if not after_window.empty else None
    peak_row = after_window.loc[after_window["change"].idxmax()] if not after_window.empty else None
    trough_row = after_window.loc[after_window["change"].idxmin()] if not after_window.empty else None

    volume_avg = frame[frame["time"] < event_ts].tail(20)["volume"].mean()
    event_volume = _safe_float(first_after.get("volume"), 0.0) or 0.0
    relative_volume = round(event_volume / volume_avg, 2) if volume_avg and volume_avg > 0 else None

    vwap = _safe_float(first_after.get("vwap"))
    vwap_prev = _safe_float(pre_row.get("vwap"))
    vwap_slope = round(vwap - vwap_prev, 4) if vwap is not None and vwap_prev is not None else None

    return {
        "data_status": "reconstructed",
        "price_before_publication": round(pre_price, 4),
        "first_tradable_price_after_publication": round(first_price, 4),
        "gap_percentage": round(((first_price - pre_price) / pre_price) * 100.0, 2),
        "return_15m_pct": _return_at(15),
        "return_30m_pct": _return_at(30),
        "return_1h_pct": _return_at(60),
        "close_to_close_pct": _return_at(390),
        "next_session_return_pct": _return_at(1_380),
        "return_3_sessions_pct": _return_at(3 * 1_380),
        "return_5_sessions_pct": _return_at(5 * 1_380),
        "max_favorable_move": max_favorable,
        "max_adverse_move": max_adverse,
        "time_to_peak_reaction_minutes": int(((int(peak_row["time"]) - event_ts) / 60)) if peak_row is not None else None,
        "time_to_reversal_minutes": int(((int(trough_row["time"]) - event_ts) / 60)) if trough_row is not None else None,
        "volume_vs_normal": relative_volume,
        "relative_volume": relative_volume,
        "vwap_behavior": {
            "above_vwap": first_price >= vwap if vwap is not None else None,
            "vwap_slope": vwap_slope,
            "holds": 1 if vwap is not None and first_price >= vwap else 0,
            "rejections": 1 if vwap is not None and first_price < vwap else 0,
            "distance_from_vwap_pct": round(((first_price - vwap) / first_price) * 100.0, 2) if vwap is not None and first_price else None,
            "reclaim_or_lose_with_volume": "Reclaim with volume" if vwap is not None and first_price >= vwap and (relative_volume or 0) >= 1.1 else "Lose VWAP with volume" if vwap is not None and first_price < vwap and (relative_volume or 0) >= 1.1 else "Unclear",
        },
    }


def _benchmark_return(frame: pd.DataFrame, event_ts: int, reference_price: float | None, minutes: int) -> float | None:
    if frame.empty or reference_price is None or reference_price <= 0:
        return None
    row = _nearest_before(frame, event_ts + minutes * 60)
    if not row:
        return None
    price = _safe_float(row.get("close"))
    if price is None:
        return None
    return round(((price - reference_price) / reference_price) * 100.0, 2)


def _classify_reaction(reaction: dict[str, Any]) -> str:
    if reaction.get("data_status") == "unavailable":
        return "INSUFFICIENT DATA"
    ret_1h = _safe_float(reaction.get("return_1h_pct"))
    ret_15m = _safe_float(reaction.get("return_15m_pct"))
    ret_close = _safe_float(reaction.get("close_to_close_pct"))
    rvol = _safe_float(reaction.get("relative_volume"))

    if ret_15m is None and ret_1h is None and ret_close is None:
        return "INSUFFICIENT DATA"
    if abs(ret_15m or 0.0) < 0.4 and abs(ret_1h or 0.0) < 0.5 and (rvol or 0.0) < 1.2:
        return "PRICED IN"
    if ret_15m is not None and ret_1h is not None and ret_15m > 0 and ret_1h < 0:
        return "INITIAL POSITIVE REACTION, THEN FADE"
    if ret_15m is not None and ret_1h is not None and ret_15m < 0 and ret_1h > 0:
        return "INITIAL NEGATIVE REACTION, THEN RECOVERY"
    if (ret_1h or 0) >= 2 or (ret_close or 0) >= 2:
        return "STRONGLY POSITIVE"
    if (ret_1h or 0) >= 0.75:
        return "MODERATELY POSITIVE"
    if (ret_1h or 0) <= -2 or (ret_close or 0) <= -2:
        return "STRONGLY NEGATIVE"
    if (ret_1h or 0) <= -0.75:
        return "MODERATELY NEGATIVE"
    if abs(ret_1h or 0.0) <= 0.5:
        return "NEUTRAL"
    return "MIXED"


def _position_impact_label(direction: str | None, reaction_classification: str, event_sentiment: str, reaction: dict[str, Any]) -> str:
    side = _safe_text(direction).upper()
    classification = reaction_classification.upper()
    strong_positive = classification in {"STRONGLY POSITIVE", "MODERATELY POSITIVE"}
    strong_negative = classification in {"STRONGLY NEGATIVE", "MODERATELY NEGATIVE"}
    if side == "SHORT":
        strong_positive, strong_negative = strong_negative, strong_positive

    if classification == "INSUFFICIENT DATA":
        return "INSUFFICIENT DATA"
    if classification == "PRICED IN":
        return "NEWS TOO OLD TO BE ACTIONABLE"
    if strong_positive:
        return "SUPPORTS POSITION"
    if strong_negative:
        return "CONFLICTS WITH POSITION"
    if classification in {"MIXED", "NEUTRAL", "INITIAL POSITIVE REACTION, THEN FADE", "INITIAL NEGATIVE REACTION, THEN RECOVERY"}:
        return "PARTIALLY SUPPORTS POSITION" if side in {"LONG", "SHORT"} else "NEUTRAL"
    return "WAIT FOR MARKET CONFIRMATION"


def _confirmation_and_invalidation(direction: str | None, reaction: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    pre = _safe_float(reaction.get("price_before_publication"))
    first = _safe_float(reaction.get("first_tradable_price_after_publication"))
    if pre is None or first is None:
        return (
            {"price": None, "condition": "No confirmed level yet."},
            {"price": None, "condition": "No confirmed level yet."},
        )
    bullish = first >= pre
    side = _safe_text(direction).upper()
    if side == "SHORT":
        bullish = not bullish
    if bullish:
        return (
            {"price": round(max(pre, first), 4), "condition": "Confirm only if price holds above the event reaction high on a completed 15-minute candle."},
            {"price": round(min(pre, first), 4), "condition": "Invalidate if price loses the event reaction low or reclaims the failed breakdown."},
        )
    return (
        {"price": round(min(pre, first), 4), "condition": "Confirm only if price holds below the event reaction low on a completed 15-minute candle."},
        {"price": round(max(pre, first), 4), "condition": "Invalidate if price reclaims the event reaction high or VWAP."},
    )


def _lookup_candles(symbol: str, indicator_data: dict[str, Any] | None, *, historical: bool, period: str = "60d") -> dict[str, Any]:
    if isinstance(indicator_data, dict) and indicator_data.get("candles"):
        return indicator_data
    try:
        df = get_candles_from_sql(symbol, "15m", period=period) if historical else fetch_candles(symbol, interval="15m", period=period, historical=historical, prefer_stored=True)
    except Exception:
        return {"symbol": normalize_symbol(symbol), "candles": [], "indicators": [], "latest": {}, "warnings": ["Historical candle data is unavailable."]}

    candles: list[dict[str, Any]] = []
    indicators: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
        candles.append(
            {
                "time": ts,
                "open": _safe_float(row.get("open"), 0.0),
                "high": _safe_float(row.get("high"), 0.0),
                "low": _safe_float(row.get("low"), 0.0),
                "close": _safe_float(row.get("close"), 0.0),
                "volume": _safe_float(row.get("volume"), 0.0),
            }
        )
        indicators.append(
            {
                "time": ts,
                "vwap": _safe_float(row.get("vwap")),
                "ema_9": _safe_float(row.get("ema_fast")),
                "ema_21": _safe_float(row.get("ema_slow")),
                "ema_50": _safe_float(row.get("ema_trend")),
                "ema_200": _safe_float(row.get("ema_200")),
                "atr": _safe_float(row.get("atr")),
                "volume_avg": _safe_float(row.get("volume_avg")),
                "rsi": _safe_float(row.get("rsi")),
            }
        )
    return {
        "symbol": normalize_symbol(symbol),
        "provider": df.attrs.get("provider") or "sqlite",
        "source": df.attrs.get("source") or "sqlite",
        "timestamp": df.attrs.get("timestamp") or df.attrs.get("last_updated"),
        "last_updated": df.attrs.get("timestamp") or df.attrs.get("last_updated"),
        "candles": candles,
        "indicators": indicators,
        "latest": indicators[-1] if indicators else {},
        "warnings": [],
    }


def _benchmark_frames(reference_ts: int, historical: bool) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in ("SPY", "QQQ"):
        try:
            df = get_candles_from_sql(symbol, "15m", period="60d") if historical else fetch_candles(symbol, interval="15m", period="60d", historical=historical, prefer_stored=True)
            if not df.empty:
                df = df.reset_index()
                if "time" not in df.columns:
                    index_name = df.columns[0]
                    df = df.rename(columns={index_name: "time"})
                if "time" in df.columns:
                    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
                    df = df.dropna(subset=["time"]).copy()
                    df["time"] = df["time"].astype("int64") // 10**9
            frames[symbol] = df
        except Exception:
            frames[symbol] = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    return frames


def _news_sources_payload(items: list[dict[str, Any]]) -> list[str]:
    return sorted({str(item.get("source") or "") for item in items if item.get("source")})


def _load_snapshot(key: str, ttl_seconds: int) -> dict[str, Any] | None:
    now = time.time()
    cached = _MEM_CACHE.get(key)
    if cached and cached.get("expires_at", 0) > now:
        return cached["payload"]
    try:
        from .db import SessionLocal

        db = SessionLocal()
        try:
            row = db.query(NewsCatalystSnapshot).filter(NewsCatalystSnapshot.key == key).first()
            if row:
                updated = _parse_timestamp(row.updated_at)
                if updated and (_now_utc() - updated).total_seconds() <= ttl_seconds:
                    payload = json.loads(row.payload_json)
                    _MEM_CACHE[key] = {"payload": payload, "expires_at": now + ttl_seconds}
                    return payload
        finally:
            db.close()
    except Exception:
        return None
    return None


def _store_snapshot(key: str, payload: dict[str, Any], symbol: str, context_type: str, ttl_seconds: int) -> None:
    now = time.time()
    _MEM_CACHE[key] = {"payload": payload, "expires_at": now + ttl_seconds}
    try:
        from .db import SessionLocal

        db = SessionLocal()
        try:
            row = db.query(NewsCatalystSnapshot).filter(NewsCatalystSnapshot.key == key).first()
            if row:
                row.payload_json = json.dumps(payload)
                row.updated_at = _now_iso()
            else:
                db.add(
                    NewsCatalystSnapshot(
                        key=key,
                        symbol=normalize_symbol(symbol),
                        context_type=context_type,
                        payload_json=json.dumps(payload),
                        created_at=_now_iso(),
                        updated_at=_now_iso(),
                    )
                )
            db.commit()
        finally:
            db.close()
    except Exception:
        return


def _item_relevance(symbol: str, item: dict[str, Any], now_et: datetime) -> float:
    symbols = {str(s).upper() for s in item.get("symbols") or []}
    title = _safe_text(item.get("title"))
    summary = _safe_text(item.get("summary"))
    published = _to_et(item.get("published_at"))
    score = 0.0
    if symbol in symbols:
        score += 60.0
    if symbol and symbol.lower() in f"{title} {summary}".lower():
        score += 20.0
    if published:
        age_days = max(0.0, (now_et - published).total_seconds() / 86400.0)
        score += max(0.0, 25.0 - age_days * 3.0)
    if "earnings" in _event_category(title, summary).lower():
        score += 10.0
    return round(score, 2)


def _should_include_item(
    item: dict[str, Any],
    *,
    symbol: str,
    lookback_days: int,
    future_days: int,
    historical: bool,
    entry_ts: str | None,
    exit_ts: str | None,
) -> bool:
    published = _to_et(item.get("published_at"))
    if not published:
        return False
    now_et = _now_utc().astimezone(EASTERN)
    if historical and entry_ts:
        entry = _to_et(entry_ts)
        exit_dt = _to_et(exit_ts) if exit_ts else None
        if entry and exit_dt:
            lower = entry - timedelta(days=lookback_days)
            upper = exit_dt + timedelta(days=2)
            return lower <= published <= upper
        if entry:
            return entry - timedelta(days=lookback_days) <= published <= entry + timedelta(days=future_days)
    if published < now_et - timedelta(days=lookback_days):
        return False
    return published <= now_et + timedelta(days=future_days)


def _build_event(
    *,
    symbol: str,
    item: dict[str, Any],
    frame: pd.DataFrame,
    benchmark_frames: dict[str, pd.DataFrame],
    market_session: dict[str, Any],
    direction: str | None,
    historical: bool,
    entry_ts: str | None,
    exit_ts: str | None,
) -> dict[str, Any]:
    title = _safe_text(item.get("title"))
    summary = _safe_text(item.get("summary"))
    source = _safe_text(item.get("source")) or "news"
    published = _to_et(item.get("published_at"))
    published_ts = int(published.timestamp()) if published else None
    relevance = _item_relevance(symbol, item, _now_utc().astimezone(EASTERN))
    category = _event_category(title, summary, source)
    sentiment = _headline_sentiment(title, summary)
    cleaned = _clean_event_text(title, summary, symbol)
    reaction = _reaction_windows(frame, published_ts) if published_ts is not None else {"data_status": "unavailable", "reason": "Missing publication timestamp."}
    reaction_classification = _classify_reaction(reaction)
    spy = benchmark_frames.get("SPY", pd.DataFrame())
    qqq = benchmark_frames.get("QQQ", pd.DataFrame())
    pre_price = _safe_float(reaction.get("price_before_publication"))
    first_price = _safe_float(reaction.get("first_tradable_price_after_publication"))
    spy_rel = _benchmark_return(spy, published_ts or 0, pre_price, 60) if published_ts is not None else None
    qqq_rel = _benchmark_return(qqq, published_ts or 0, pre_price, 60) if published_ts is not None else None
    market_adjusted = {
        "ticker_return_minus_spy_return": round((reaction.get("return_1h_pct") or 0) - (spy_rel or 0), 2) if reaction.get("return_1h_pct") is not None and spy_rel is not None else None,
        "ticker_return_minus_qqq_return": round((reaction.get("return_1h_pct") or 0) - (qqq_rel or 0), 2) if reaction.get("return_1h_pct") is not None and qqq_rel is not None else None,
        "ticker_return_minus_sector_return": None,
    }
    impact_label = _position_impact_label(direction, reaction_classification, sentiment, reaction)
    confirmation, invalidation = _confirmation_and_invalidation(direction, reaction)
    volume_response = "Above normal" if (reaction.get("relative_volume") or 0) >= 1.25 else "Near normal" if (reaction.get("relative_volume") or 0) >= 0.8 else "Light"

    first_after = _nearest_after(frame, published_ts or 0) if published_ts is not None else None
    marker_time = published_ts
    marker_text = cleaned[:20] if cleaned else "News"
    if marker_time is None and first_after is not None:
        marker_time = int(first_after.get("time") or 0)

    return {
        "headline": title,
        "summary": summary,
        "source": source,
        "url": _safe_text(item.get("link")),
        "publication_timestamp": item.get("published_at"),
        "retrieval_timestamp": _now_iso(),
        "event_category": category,
        "relevance_score": relevance,
        "confidence": "HIGH" if relevance >= 70 else "MEDIUM" if relevance >= 40 else "LOW",
        "report_type": "confirmed" if category == "earnings" else "analysis",
        "original_source": source,
        "first_publication_time": item.get("published_at"),
        "later_updates": [],
        "headline_sentiment": sentiment,
        "reaction": {
            **reaction,
            "market_adjusted": market_adjusted,
            "spy_relative_return_pct": spy_rel,
            "qqq_relative_return_pct": qqq_rel,
        },
        "news_reaction_classification": reaction_classification,
        "position_impact": impact_label,
        "confirmation_level": confirmation,
        "invalidation_level": invalidation,
        "price_marker": {
            "time": marker_time,
            "position": "aboveBar" if _safe_text(direction).upper() != "SHORT" else "belowBar",
            "color": "#60a5fa" if sentiment != "negative" else "#f59e0b",
            "shape": "circle",
            "text": marker_text,
        },
        "market_session": market_session.get("session_state") if market_session else None,
        "session_note": market_session.get("session_note") if market_session else None,
        "data_status": reaction.get("data_status"),
        "why_it_matters": f"{category.capitalize()} event. Headline sentiment was {sentiment}, but price reaction determines the actual trading impact.",
        "actual_share_price_reaction": {
            "price_before_publication": reaction.get("price_before_publication"),
            "first_tradable_price_after_publication": reaction.get("first_tradable_price_after_publication"),
            "gap_percentage": reaction.get("gap_percentage"),
            "return_15m_pct": reaction.get("return_15m_pct"),
            "return_30m_pct": reaction.get("return_30m_pct"),
            "return_1h_pct": reaction.get("return_1h_pct"),
            "close_to_close_pct": reaction.get("close_to_close_pct"),
            "next_session_return_pct": reaction.get("next_session_return_pct"),
            "return_3_sessions_pct": reaction.get("return_3_sessions_pct"),
            "return_5_sessions_pct": reaction.get("return_5_sessions_pct"),
            "max_favorable_move": reaction.get("max_favorable_move"),
            "max_adverse_move": reaction.get("max_adverse_move"),
            "time_to_peak_reaction_minutes": reaction.get("time_to_peak_reaction_minutes"),
            "time_to_reversal_minutes": reaction.get("time_to_reversal_minutes"),
            "volume_vs_normal": reaction.get("volume_vs_normal"),
            "relative_volume": reaction.get("relative_volume"),
            "vwap_behavior": reaction.get("vwap_behavior"),
            "source": "observed" if reaction.get("data_status") == "reconstructed" else reaction.get("data_status"),
            "timestamp": item.get("published_at"),
            "confidence": "HIGH" if reaction.get("data_status") != "unavailable" else "LOW",
        },
    }


def _relevant_events(
    symbol: str,
    *,
    historical: bool,
    entry_ts: str | None,
    exit_ts: str | None,
    market_session: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    cfg = config_manager.get("news", default={}) or {}
    lookback_days = int(cfg.get("catalyst_lookback_days", 7) or 7)
    future_days = int(cfg.get("catalyst_future_days", 14) or 14)
    major_days = int(cfg.get("catalyst_major_lookback_days", 30) or 30)
    if historical:
        lookback_days = int(cfg.get("historical_lookback_trading_days", 5) or 5)
        future_days = int(cfg.get("historical_post_exit_days", 2) or 2)

    news_payload = market_news_feed()
    earnings_payload = upcoming_earnings_feed()
    feed_items = list(news_payload.get("items") or [])
    earnings_items = list(earnings_payload.get("items") or [])
    now_et = _now_utc().astimezone(EASTERN)

    symbol = normalize_symbol(symbol)
    selected: list[dict[str, Any]] = []
    for item in feed_items:
        symbols = {str(s).upper() for s in item.get("symbols") or []}
        if symbol not in symbols:
            continue
        if _should_include_item(item, symbol=symbol, lookback_days=lookback_days, future_days=future_days, historical=historical, entry_ts=entry_ts, exit_ts=exit_ts):
            selected.append(item)

    upcoming: list[dict[str, Any]] = []
    for item in earnings_items:
        if _safe_text(item.get("symbol")).upper() != symbol:
            continue
        published = _to_et(item.get("event_time_et"))
        if not published:
            continue
        if historical:
            exit_dt = _to_et(exit_ts) if exit_ts else None
            entry_dt = _to_et(entry_ts) if entry_ts else None
            if entry_dt and exit_dt and not (entry_dt - timedelta(days=lookback_days) <= published <= exit_dt + timedelta(days=future_days)):
                continue
        elif not (now_et <= published <= now_et + timedelta(days=future_days)):
            continue
        upcoming.append(item)

    # Group duplicates by normalized headline.
    grouped: dict[str, dict[str, Any]] = {}
    for item in selected:
        key = _normalize_headline(item.get("title") or item.get("summary"))
        existing = grouped.get(key)
        if not existing:
            grouped[key] = {
                **item,
                "sources": [item.get("source")],
                "links": [item.get("link")] if item.get("link") else [],
                "updates": [],
            }
            continue
        existing.setdefault("sources", []).append(item.get("source"))
        if item.get("link"):
            existing.setdefault("links", []).append(item.get("link"))
        existing.setdefault("updates", []).append(
            {
                "source": item.get("source"),
                "published_at": item.get("published_at"),
                "title": item.get("title"),
            }
        )
    deduped = list(grouped.values())
    deduped.sort(key=lambda row: _to_et(row.get("published_at")) or _now_utc(), reverse=True)
    return deduped, upcoming, _news_sources_payload(feed_items + earnings_items)


def _build_upcoming_catalysts(upcoming: list[dict[str, Any]], expiration: str | None) -> list[dict[str, Any]]:
    expiration_dt = _parse_timestamp(expiration)
    if expiration_dt is None:
        try:
            expiration_dt = datetime.fromisoformat(str(expiration).strip()).replace(tzinfo=EASTERN).astimezone(timezone.utc) if expiration else None
        except Exception:
            expiration_dt = None
    output: list[dict[str, Any]] = []
    for item in upcoming:
        event_dt = _to_et(item.get("event_time_et"))
        if not event_dt:
            continue
        output.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "event_type": "earnings",
                "date_time": item.get("event_time_et"),
                "occurs_before_expiration": bool(expiration_dt and event_dt <= expiration_dt),
                "historical_average_move": None,
                "current_options_implied_move": None,
                "overnight_gap_risk": True,
                "source": item.get("source"),
            }
        )
    return output


def build_news_catalyst_impact(
    symbol: str,
    *,
    market_session: dict[str, Any] | None = None,
    indicator_data: dict[str, Any] | None = None,
    historical: bool = False,
    direction: str | None = None,
    entry_ts: str | None = None,
    exit_ts: str | None = None,
    expiration: str | None = None,
    context_type: str = "candidate",
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized:
        return {
            "symbol": normalized,
            "context_type": context_type,
            "data_status": "unavailable",
            "summary": {
                "headline": "No symbol provided",
                "impact_label": "INSUFFICIENT DATA",
                "most_relevant_event": None,
                "last_news_refresh": _now_iso(),
                "latest_relevant_event_timestamp": None,
                "market_session": (market_session or get_market_session()).get("session_state"),
                "price_data_timestamp": None,
                "source_list": [],
                "confidence": "LOW",
                "note": "No news impact can be calculated without a symbol.",
            },
            "events": [],
            "upcoming_catalysts": [],
            "news_markers": [],
        }

    session = market_session or get_market_session()
    ttl_seconds = market_aware_ttl(int(config_manager.get("news", "cache_ttl_seconds", default=180) or 180), market_session=session)
    key = _factor_key(normalized, context_type, entry_ts, exit_ts)
    cached = _load_snapshot(key, ttl_seconds)
    if cached:
        return cached

    frame_data = _lookup_candles(normalized, indicator_data, historical=historical)
    frame = _records_to_frame(frame_data)
    if frame.empty:
        # Try a broad historical fetch if the supplied data is thin.
        frame_data = _lookup_candles(normalized, None, historical=historical)
        frame = _records_to_frame(frame_data)

    events_raw, upcoming_raw, sources = _relevant_events(
        normalized,
        historical=historical,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        market_session=session,
    )

    benchmark_frames = _benchmark_frames(int(frame["time"].iloc[-1]) if not frame.empty else int(_now_utc().timestamp()), historical=historical)
    events: list[dict[str, Any]] = []
    for item in events_raw:
        event = _build_event(
            symbol=normalized,
            item=item,
            frame=frame,
            benchmark_frames=benchmark_frames,
            market_session=session,
            direction=direction,
            historical=historical,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
        )
        events.append(event)

    events.sort(key=lambda row: (
        -float(row.get("relevance_score") or 0.0),
        _to_et(row.get("publication_timestamp")) or _now_utc(),
    ))
    upcoming_catalysts = _build_upcoming_catalysts(upcoming_raw, expiration)
    most_relevant = events[0] if events else None
    latest_event_ts = None
    if most_relevant:
        latest_event_ts = most_relevant.get("publication_timestamp")

    if session and not session.get("actionable_live_quotes"):
        session_note = "News analysis is for next-session planning. Refresh price, options, and positioning data after the market opens."
    else:
        session_note = "News analysis uses the latest available price and catalyst data."

    if most_relevant:
        impact_label = most_relevant.get("position_impact") or "INSUFFICIENT DATA"
        confidence = most_relevant.get("confidence") or "LOW"
    else:
        impact_label = "INSUFFICIENT DATA"
        confidence = "LOW"

    payload = {
        "symbol": normalized,
        "context_type": context_type,
        "market_session": session.get("session_state") if session else None,
        "session_note": session_note,
        "data_status": "observed" if events else "unavailable",
        "data_freshness": "LIVE" if session and session.get("actionable_live_quotes") else "PREVIOUS_SESSION",
        "last_news_refresh": _now_iso(),
        "latest_relevant_event_timestamp": latest_event_ts,
        "source_list": sources,
        "confidence": confidence,
        "impact_label": impact_label,
        "summary": {
            "headline": most_relevant.get("headline") if most_relevant else "No material recent catalyst found",
            "plain_english_summary": most_relevant.get("why_it_matters") if most_relevant else "No relevant recent catalyst was identified from the configured news sources.",
            "why_it_matters": most_relevant.get("why_it_matters") if most_relevant else "No relevant recent catalyst was identified from the configured news sources.",
            "most_relevant_event": most_relevant,
            "publication_time": most_relevant.get("publication_timestamp") if most_relevant else None,
            "actual_share_price_reaction": most_relevant.get("actual_share_price_reaction") if most_relevant else None,
            "market_adjusted_reaction": most_relevant.get("actual_share_price_reaction", {}).get("market_adjusted") if most_relevant else None,
            "volume_response": "Above normal" if most_relevant and (most_relevant.get("actual_share_price_reaction", {}).get("relative_volume") or 0) >= 1.25 else "Near normal" if most_relevant else None,
            "options_response": "unavailable",
            "position_impact": impact_label,
            "confirmation_level": most_relevant.get("confirmation_level") if most_relevant else {"price": None, "condition": "No confirmed level yet."},
            "invalidation_level": most_relevant.get("invalidation_level") if most_relevant else {"price": None, "condition": "No confirmed level yet."},
            "upcoming_catalysts": upcoming_catalysts,
            "data_confidence": confidence,
            "market_session": session.get("session_state") if session else None,
            "price_data_timestamp": frame_data.get("timestamp") if isinstance(frame_data, dict) else None,
            "source_list": sources,
            "latest_relevant_event_timestamp": latest_event_ts,
            "news_markers": [event.get("price_marker") for event in events if event.get("price_marker")],
        },
        "events": events,
        "upcoming_catalysts": upcoming_catalysts,
        "news_markers": [event.get("price_marker") for event in events if event.get("price_marker")],
    }
    _store_snapshot(key, payload, normalized, context_type, ttl_seconds)
    return payload
