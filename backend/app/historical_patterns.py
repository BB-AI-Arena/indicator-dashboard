from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import desc
from sqlalchemy.orm import Session

from .config import config_manager
from .db import SessionLocal
from .history import get_candles_from_sql, interval_seconds, now_iso
from .indicators import apply_indicators
from .models import (
    HistoricalSetupFeature,
    HistoricalSetupFamily,
    HistoricalSetupOutcome,
    OptionPositioningSnapshot,
    Watchlist,
)
from .options import ranked_contracts


EASTERN = ZoneInfo("America/New_York")
FEATURE_VERSION_DEFAULT = "setup_features_v1"
OUTCOME_VERSION_DEFAULT = "setup_outcomes_v1"
MATCHING_VERSION_DEFAULT = "weighted_knn_v1"

NUMERIC_WEIGHTS = {
    "close_vwap_atr": 1.35,
    "vwap_slope_atr": 1.1,
    "ema_fast_slow_atr": 1.2,
    "ema_slow_trend_atr": 1.0,
    "close_ema_fast_atr": 0.75,
    "close_ema_slow_atr": 0.75,
    "close_ema_trend_atr": 0.75,
    "rsi_norm": 0.85,
    "rsi_slope_norm": 0.55,
    "macd_hist_atr": 0.85,
    "bb_position": 0.55,
    "relative_volume": 0.85,
    "body_pct": 0.45,
    "upper_wick_pct": 0.35,
    "lower_wick_pct": 0.35,
    "distance_support_atr": 0.75,
    "distance_resistance_atr": 0.75,
    "obv_slope_norm": 0.55,
    "cmf": 0.5,
    "mfi_norm": 0.45,
    "gap_pct_norm": 0.35,
    "spy_relative_return": 0.65,
    "qqq_relative_return": 0.65,
}

HORIZON_BARS = {
    "next_15m": 1,
    "30m": 2,
    "60m": 4,
    "2h": 8,
    "session_close": 26,
    "next_session": 52,
    "3_sessions": 78,
    "5_sessions": 130,
}


@dataclass
class FeatureExample:
    symbol: str
    interval: str
    timestamp: int
    index_position: int
    setup_family: str
    direction: str
    setup_state: str
    data_quality: str
    features: dict[str, Any]
    vector: dict[str, float]


def _cfg() -> dict[str, Any]:
    return config_manager.get("historical_patterns", default={}) or {}


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _clip(value: float | None, lower: float = -5.0, upper: float = 5.0) -> float:
    if value is None:
        return 0.0
    return max(lower, min(upper, float(value)))


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (float(numerator) / float(denominator)) * 100.0


def _ratio_delta(a: float | None, b: float | None, scale: float) -> float:
    if a is None or b is None or not scale:
        return 0.0
    return _clip((float(a) - float(b)) / float(scale))


def _timestamp(index_value: Any) -> int:
    if hasattr(index_value, "timestamp"):
        return int(index_value.timestamp())
    return int(index_value)


def _iso_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def _confidence_from_sample(n: int) -> str:
    cfg = _cfg()
    if n < int(cfg.get("min_examples", 10) or 10):
        return "INSUFFICIENT"
    if n < int(cfg.get("moderate_examples", 30) or 30):
        return "LOW"
    if n < int(cfg.get("strong_examples", 100) or 100):
        return "MODERATE"
    return "STRONG"


def wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, float | None]:
    if total <= 0:
        return {"low": None, "high": None}
    phat = successes / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    spread = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return {
        "low": max(0.0, (centre - spread) / denominator),
        "high": min(1.0, (centre + spread) / denominator),
    }


def _add_flow_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    direction = np.sign(out["close"].diff().fillna(0.0))
    out["obv"] = (direction * out["volume"].fillna(0.0)).cumsum()
    out["obv_slope"] = out["obv"].diff(5)

    high_low = (out["high"] - out["low"]).replace(0, np.nan)
    money_flow_multiplier = (((out["close"] - out["low"]) - (out["high"] - out["close"])) / high_low).fillna(0.0)
    money_flow_volume = money_flow_multiplier * out["volume"].fillna(0.0)
    out["cmf"] = money_flow_volume.rolling(20, min_periods=5).sum() / out["volume"].rolling(20, min_periods=5).sum().replace(0, np.nan)

    typical = (out["high"] + out["low"] + out["close"]) / 3
    raw_flow = typical * out["volume"].fillna(0.0)
    positive_flow = raw_flow.where(typical > typical.shift(1), 0.0)
    negative_flow = raw_flow.where(typical < typical.shift(1), 0.0)
    flow_ratio = positive_flow.rolling(14, min_periods=5).sum() / negative_flow.rolling(14, min_periods=5).sum().replace(0, np.nan)
    out["mfi"] = 100 - (100 / (1 + flow_ratio))
    return out.replace([np.inf, -np.inf], np.nan)


def _add_structure_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["support"] = out["low"].shift(1).rolling(20, min_periods=5).min()
    out["resistance"] = out["high"].shift(1).rolling(20, min_periods=5).max()
    out["prev_close"] = out["close"].shift(1)
    out["gap_pct"] = ((out["open"] - out["prev_close"]) / out["prev_close"].replace(0, np.nan)) * 100.0

    index_et = out.index.tz_convert(EASTERN) if getattr(out.index, "tz", None) is not None else out.index.tz_localize("UTC").tz_convert(EASTERN)
    out["_et_date"] = [idx.date().isoformat() for idx in index_et]
    out["_minutes_from_open"] = [max(0, (idx.hour * 60 + idx.minute) - (9 * 60 + 30)) for idx in index_et]
    out["_weekday"] = [idx.weekday() for idx in index_et]

    daily = out.groupby("_et_date").agg(day_high=("high", "max"), day_low=("low", "min"), day_close=("close", "last"))
    daily["prev_day_high"] = daily["day_high"].shift(1)
    daily["prev_day_low"] = daily["day_low"].shift(1)
    daily["prev_day_close"] = daily["day_close"].shift(1)
    prev_map = daily[["prev_day_high", "prev_day_low", "prev_day_close"]].to_dict(orient="index")
    out["prev_day_high"] = [prev_map.get(day, {}).get("prev_day_high") for day in out["_et_date"]]
    out["prev_day_low"] = [prev_map.get(day, {}).get("prev_day_low") for day in out["_et_date"]]
    out["prev_day_close"] = [prev_map.get(day, {}).get("prev_day_close") for day in out["_et_date"]]

    opening_ranges: dict[str, dict[str, float]] = {}
    for day, rows in out.groupby("_et_date", sort=False):
        regular = rows[rows["_minutes_from_open"].between(0, 30, inclusive="left")]
        source = regular if not regular.empty else rows.head(2)
        opening_ranges[day] = {
            "opening_range_high": float(source["high"].max()) if not source.empty else float("nan"),
            "opening_range_low": float(source["low"].min()) if not source.empty else float("nan"),
        }
    out["opening_range_high"] = [opening_ranges.get(day, {}).get("opening_range_high") for day in out["_et_date"]]
    out["opening_range_low"] = [opening_ranges.get(day, {}).get("opening_range_low") for day in out["_et_date"]]

    higher_high = out["high"] > out["high"].shift(1)
    higher_low = out["low"] > out["low"].shift(1)
    lower_high = out["high"] < out["high"].shift(1)
    lower_low = out["low"] < out["low"].shift(1)
    out["consecutive_higher_highs"] = higher_high.groupby((higher_high != higher_high.shift()).cumsum()).cumcount() + 1
    out["consecutive_higher_lows"] = higher_low.groupby((higher_low != higher_low.shift()).cumsum()).cumcount() + 1
    out["consecutive_lower_highs"] = lower_high.groupby((lower_high != lower_high.shift()).cumsum()).cumcount() + 1
    out["consecutive_lower_lows"] = lower_low.groupby((lower_low != lower_low.shift()).cumsum()).cumcount() + 1
    for col, mask in {
        "consecutive_higher_highs": higher_high,
        "consecutive_higher_lows": higher_low,
        "consecutive_lower_highs": lower_high,
        "consecutive_lower_lows": lower_low,
    }.items():
        out.loc[~mask, col] = 0

    return out.replace([np.inf, -np.inf], np.nan)


def _load_enriched(symbol: str, interval: str, period: str, db: Session) -> pd.DataFrame:
    df = get_candles_from_sql(symbol, interval, period=period, db=db)
    if df.empty:
        return df
    indicators = apply_indicators(df, config_manager.get("indicators", default={}) or {})
    enriched = _add_structure_columns(_add_flow_columns(indicators))
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=interval_seconds(interval))
    return enriched[enriched.index <= cutoff].copy()


def _benchmark_relative(symbol_frame: pd.DataFrame, benchmark_frame: pd.DataFrame) -> pd.Series:
    if symbol_frame.empty or benchmark_frame.empty:
        return pd.Series(0.0, index=symbol_frame.index)
    base = benchmark_frame["close"].reindex(symbol_frame.index, method="nearest", tolerance=pd.Timedelta(minutes=20))
    sym_ret = symbol_frame["close"].pct_change(4).fillna(0.0) * 100.0
    bench_ret = base.pct_change(4).fillna(0.0) * 100.0
    return (sym_ret - bench_ret).fillna(0.0)


def _setup_side_and_family(row: pd.Series, prev: pd.Series | None) -> tuple[str, str, str]:
    close = _safe_float(row.get("close"), 0.0) or 0.0
    vwap = _safe_float(row.get("vwap"))
    ema_fast = _safe_float(row.get("ema_fast"))
    ema_slow = _safe_float(row.get("ema_slow"))
    ema_trend = _safe_float(row.get("ema_trend"))
    rsi = _safe_float(row.get("rsi"), 50.0) or 50.0
    macd_hist = _safe_float(row.get("macd_hist"), 0.0) or 0.0
    resistance = _safe_float(row.get("resistance"))
    support = _safe_float(row.get("support"))
    rel_vol = _safe_float(row.get("relative_volume"), 1.0) or 1.0

    long_score = 0
    short_score = 0
    if vwap is not None:
        long_score += int(close > vwap)
        short_score += int(close < vwap)
    if ema_fast is not None and ema_slow is not None:
        long_score += int(ema_fast > ema_slow)
        short_score += int(ema_fast < ema_slow)
    if ema_slow is not None and ema_trend is not None:
        long_score += int(ema_slow > ema_trend)
        short_score += int(ema_slow < ema_trend)
    long_score += int(macd_hist > 0)
    short_score += int(macd_hist < 0)
    long_score += int(52 <= rsi <= 72)
    short_score += int(28 <= rsi <= 48)
    long_score += int(rel_vol >= 1.2)
    short_score += int(rel_vol >= 1.2)

    direction = "LONG" if long_score >= short_score else "SHORT"
    state = "CONFIRMING" if max(long_score, short_score) >= 5 else "FORMING" if max(long_score, short_score) >= 3 else "DATA INSUFFICIENT"

    prev_close = _safe_float(prev.get("close")) if prev is not None else None
    prev_vwap = _safe_float(prev.get("vwap")) if prev is not None else None
    if direction == "LONG":
        if prev_close is not None and prev_vwap is not None and vwap is not None and prev_close <= prev_vwap and close > vwap:
            family = "VWAP reclaim continuation"
        elif resistance is not None and close > resistance:
            family = "relative-strength breakout"
        elif ema_fast is not None and ema_slow is not None and close > ema_slow and ema_fast > ema_slow:
            family = "EMA continuation"
        else:
            family = "bullish structure continuation"
    else:
        if prev_close is not None and prev_vwap is not None and vwap is not None and prev_close >= prev_vwap and close < vwap:
            family = "VWAP rejection continuation"
        elif support is not None and close < support:
            family = "relative-weakness breakdown"
        elif ema_fast is not None and ema_slow is not None and close < ema_slow and ema_fast < ema_slow:
            family = "EMA rejection"
        else:
            family = "bearish structure continuation"
    return direction, family, state


def _feature_from_row(symbol: str, interval: str, frame: pd.DataFrame, position: int, spy_rel: pd.Series, qqq_rel: pd.Series) -> FeatureExample | None:
    if position <= 0 or position >= len(frame):
        return None
    row = frame.iloc[position]
    prev = frame.iloc[position - 1]
    close = _safe_float(row.get("close"))
    atr = _safe_float(row.get("atr"))
    if close is None or close <= 0 or atr is None or atr <= 0:
        return None

    high = _safe_float(row.get("high"), close) or close
    low = _safe_float(row.get("low"), close) or close
    open_price = _safe_float(row.get("open"), close) or close
    candle_range = max(0.000001, high - low)
    body = abs(close - open_price)
    volume = _safe_float(row.get("volume"), 0.0) or 0.0
    volume_avg = _safe_float(row.get("volume_avg"), 0.0) or 0.0
    rel_vol = volume / volume_avg if volume_avg > 0 else 0.0
    row = row.copy()
    row["relative_volume"] = rel_vol
    direction, family, state = _setup_side_and_family(row, prev)
    ts = _timestamp(frame.index[position])

    bb_upper = _safe_float(row.get("bb_upper"))
    bb_lower = _safe_float(row.get("bb_lower"))
    bb_position = 0.5
    if bb_upper is not None and bb_lower is not None and bb_upper != bb_lower:
        bb_position = _clip((close - bb_lower) / (bb_upper - bb_lower), -1.0, 2.0)

    vector = {
        "close_vwap_atr": _ratio_delta(close, _safe_float(row.get("vwap")), atr),
        "vwap_slope_atr": _ratio_delta(_safe_float(row.get("vwap")), _safe_float(frame.iloc[max(0, position - 3)].get("vwap")), atr),
        "ema_fast_slow_atr": _ratio_delta(_safe_float(row.get("ema_fast")), _safe_float(row.get("ema_slow")), atr),
        "ema_slow_trend_atr": _ratio_delta(_safe_float(row.get("ema_slow")), _safe_float(row.get("ema_trend")), atr),
        "close_ema_fast_atr": _ratio_delta(close, _safe_float(row.get("ema_fast")), atr),
        "close_ema_slow_atr": _ratio_delta(close, _safe_float(row.get("ema_slow")), atr),
        "close_ema_trend_atr": _ratio_delta(close, _safe_float(row.get("ema_trend")), atr),
        "rsi_norm": _clip(((_safe_float(row.get("rsi"), 50.0) or 50.0) - 50.0) / 50.0, -1.0, 1.0),
        "rsi_slope_norm": _clip(((_safe_float(row.get("rsi"), 50.0) or 50.0) - (_safe_float(frame.iloc[max(0, position - 3)].get("rsi"), 50.0) or 50.0)) / 20.0, -2.0, 2.0),
        "macd_hist_atr": _clip((_safe_float(row.get("macd_hist"), 0.0) or 0.0) / atr),
        "bb_position": float(bb_position),
        "relative_volume": _clip(rel_vol, 0.0, 5.0),
        "body_pct": _clip(body / candle_range, 0.0, 1.0),
        "upper_wick_pct": _clip((high - max(open_price, close)) / candle_range, 0.0, 1.0),
        "lower_wick_pct": _clip((min(open_price, close) - low) / candle_range, 0.0, 1.0),
        "distance_support_atr": _ratio_delta(close, _safe_float(row.get("support")), atr),
        "distance_resistance_atr": _ratio_delta(_safe_float(row.get("resistance")), close, atr),
        "obv_slope_norm": _clip((_safe_float(row.get("obv_slope"), 0.0) or 0.0) / max(volume_avg, 1.0), -5.0, 5.0),
        "cmf": _clip(_safe_float(row.get("cmf"), 0.0) or 0.0, -1.0, 1.0),
        "mfi_norm": _clip(((_safe_float(row.get("mfi"), 50.0) or 50.0) - 50.0) / 50.0, -1.0, 1.0),
        "gap_pct_norm": _clip((_safe_float(row.get("gap_pct"), 0.0) or 0.0) / 5.0, -3.0, 3.0),
        "spy_relative_return": _clip(float(spy_rel.iloc[position]) / 5.0 if len(spy_rel) > position else 0.0, -3.0, 3.0),
        "qqq_relative_return": _clip(float(qqq_rel.iloc[position]) / 5.0 if len(qqq_rel) > position else 0.0, -3.0, 3.0),
    }

    unavailable = []
    for key in ["vwap", "ema_fast", "ema_slow", "ema_trend", "rsi", "macd_hist", "cmf", "mfi"]:
        if _safe_float(row.get(key)) is None:
            unavailable.append(key)

    features = {
        "price": close,
        "atr": atr,
        "atr_pct": _pct(atr, close),
        "volume": volume,
        "relative_volume": rel_vol,
        "vwap": _safe_float(row.get("vwap")),
        "ema_fast": _safe_float(row.get("ema_fast")),
        "ema_slow": _safe_float(row.get("ema_slow")),
        "ema_trend": _safe_float(row.get("ema_trend")),
        "rsi": _safe_float(row.get("rsi")),
        "macd_hist": _safe_float(row.get("macd_hist")),
        "support": _safe_float(row.get("support")),
        "resistance": _safe_float(row.get("resistance")),
        "previous_day_high": _safe_float(row.get("prev_day_high")),
        "previous_day_low": _safe_float(row.get("prev_day_low")),
        "opening_range_high": _safe_float(row.get("opening_range_high")),
        "opening_range_low": _safe_float(row.get("opening_range_low")),
        "minutes_from_open": int(row.get("_minutes_from_open") or 0),
        "day_of_week": int(row.get("_weekday") or 0),
        "candle_completed": True,
        "unavailable_features": unavailable,
        "data_status": "observed" if not unavailable else "partial",
    }

    data_quality = "HIGH" if len(unavailable) <= 1 else "MEDIUM" if len(unavailable) <= 4 else "LOW"
    return FeatureExample(symbol, interval, ts, position, family, direction, state, data_quality, features, vector)


def _indicator_flags(example: FeatureExample, side: str) -> dict[str, bool]:
    v = example.vector
    f = example.features
    side = str(side or example.direction).upper()
    if side == "SHORT":
        return {
            "price below VWAP": v.get("close_vwap_atr", 0) < 0,
            "VWAP falling": v.get("vwap_slope_atr", 0) < 0,
            "EMA fast below EMA slow": v.get("ema_fast_slow_atr", 0) < 0,
            "EMA slow below trend EMA": v.get("ema_slow_trend_atr", 0) < 0,
            "RSI in bearish range": (f.get("rsi") or 50) <= 55,
            "MACD histogram bearish": v.get("macd_hist_atr", 0) < 0,
            "volume expanded": (f.get("relative_volume") or 0) >= 1.2,
            "near support or breakdown": v.get("distance_support_atr", 0) <= 1.0,
        }
    return {
        "price above VWAP": v.get("close_vwap_atr", 0) > 0,
        "VWAP rising": v.get("vwap_slope_atr", 0) > 0,
        "EMA fast above EMA slow": v.get("ema_fast_slow_atr", 0) > 0,
        "EMA slow above trend EMA": v.get("ema_slow_trend_atr", 0) > 0,
        "RSI in bullish range": (f.get("rsi") or 50) >= 45,
        "MACD histogram bullish": v.get("macd_hist_atr", 0) > 0,
        "volume expanded": (f.get("relative_volume") or 0) >= 1.2,
        "near resistance or breakout": v.get("distance_resistance_atr", 0) <= 1.0,
    }


def _similarity(a: FeatureExample, b: FeatureExample) -> float:
    total_weight = 0.0
    weighted_distance = 0.0
    for key, weight in NUMERIC_WEIGHTS.items():
        av = float(a.vector.get(key, 0.0) or 0.0)
        bv = float(b.vector.get(key, 0.0) or 0.0)
        weighted_distance += weight * ((av - bv) ** 2)
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    distance = math.sqrt(weighted_distance / total_weight)
    score = 100.0 * math.exp(-distance)
    if a.direction == b.direction:
        score += 4.0
    if a.setup_family == b.setup_family:
        score += 4.0
    return round(max(0.0, min(100.0, score)), 2)


def _outcome_for(frame: pd.DataFrame, position: int, direction: str, horizon_bars: int) -> dict[str, Any] | None:
    cfg = _cfg()
    if position < 0 or position + horizon_bars > len(frame) - 1:
        return None
    row = frame.iloc[position]
    future = frame.iloc[position + 1 : position + horizon_bars + 1]
    if future.empty:
        return None

    entry = _safe_float(row.get("close"))
    atr = _safe_float(row.get("atr"))
    if entry is None or entry <= 0 or atr is None or atr <= 0:
        return None
    direction = str(direction or "LONG").upper()
    target_1_move = max(atr * float(cfg.get("target_1_atr", 1.0) or 1.0), entry * float(cfg.get("min_usable_move_pct", 0.35) or 0.35) / 100.0)
    target_2_move = max(atr * float(cfg.get("target_2_atr", 1.5) or 1.5), target_1_move * 1.4)
    invalidation_move = max(atr * float(cfg.get("invalidation_atr", 0.75) or 0.75), entry * float(cfg.get("min_usable_move_pct", 0.35) or 0.35) / 100.0)
    cost_pct = float(cfg.get("modeled_cost_pct", 0.4) or 0.4)

    exit_close = float(future["close"].iloc[-1])
    price_forward_return_pct = ((exit_close - entry) / entry) * 100.0
    if direction == "SHORT":
        side_return_pct = ((entry - exit_close) / entry) * 100.0
        mfe_price = float(future["low"].min())
        mae_price = float(future["high"].max())
        mfe_pct = ((entry - mfe_price) / entry) * 100.0
        mae_pct = -((mae_price - entry) / entry) * 100.0
        target_1_price = entry - target_1_move
        target_2_price = entry - target_2_move
        invalidation_price = entry + invalidation_move
        target_1_hits = future["low"] <= target_1_price
        target_2_hits = future["low"] <= target_2_price
        invalidation_hits = future["high"] >= invalidation_price
    else:
        side_return_pct = price_forward_return_pct
        mfe_price = float(future["high"].max())
        mae_price = float(future["low"].min())
        mfe_pct = ((mfe_price - entry) / entry) * 100.0
        mae_pct = -((entry - mae_price) / entry) * 100.0
        target_1_price = entry + target_1_move
        target_2_price = entry + target_2_move
        invalidation_price = entry - invalidation_move
        target_1_hits = future["high"] >= target_1_price
        target_2_hits = future["high"] >= target_2_price
        invalidation_hits = future["low"] <= invalidation_price

    def first_hit(mask: pd.Series) -> int | None:
        hits = np.flatnonzero(mask.to_numpy())
        return int(hits[0]) if len(hits) else None

    t1_idx = first_hit(target_1_hits)
    t2_idx = first_hit(target_2_hits)
    inv_idx = first_hit(invalidation_hits)
    mfe_idx = int(np.argmax((future["high"] if direction != "SHORT" else -future["low"]).to_numpy()))
    mae_idx = int(np.argmax((future["low"] * -1 if direction != "SHORT" else future["high"]).to_numpy()))
    minutes_per_bar = max(1, interval_seconds(str(frame.attrs.get("interval") or "15m")) // 60)
    threshold_pct = max(float(cfg.get("min_usable_move_pct", 0.35) or 0.35), (atr / entry) * 100.0 * 0.35)
    if price_forward_return_pct >= threshold_pct:
        directional_outcome = "BULLISH"
    elif price_forward_return_pct <= -threshold_pct:
        directional_outcome = "BEARISH"
    else:
        directional_outcome = "NEUTRAL"

    target_1_before = t1_idx is not None and (inv_idx is None or t1_idx < inv_idx)
    target_2_before = t2_idx is not None and (inv_idx is None or t2_idx < inv_idx)
    invalidation_before = inv_idx is not None and (t1_idx is None or inv_idx <= t1_idx)
    return {
        "entry": entry,
        "direction": direction,
        "forward_return_pct": side_return_pct,
        "price_forward_return_pct": price_forward_return_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "time_to_mfe_minutes": (mfe_idx + 1) * minutes_per_bar,
        "time_to_mae_minutes": (mae_idx + 1) * minutes_per_bar,
        "target_1_price": target_1_price,
        "target_2_price": target_2_price,
        "invalidation_price": invalidation_price,
        "target_1_reached": t1_idx is not None,
        "target_2_reached": t2_idx is not None,
        "invalidation_reached": inv_idx is not None,
        "target_1_before_invalidation": target_1_before,
        "target_2_before_invalidation": target_2_before,
        "invalidation_before_target": invalidation_before,
        "directional_outcome": directional_outcome,
        "profitable_after_costs": side_return_pct > cost_pct,
        "modeled_cost_pct": cost_pct,
    }


def _persist_feature(db: Session, example: FeatureExample) -> HistoricalSetupFeature | None:
    version = str(_cfg().get("feature_version", FEATURE_VERSION_DEFAULT) or FEATURE_VERSION_DEFAULT)
    payload = {
        "features": example.features,
        "vector": example.vector,
        "feature_version": version,
        "matching_version": str(_cfg().get("matching_version", MATCHING_VERSION_DEFAULT) or MATCHING_VERSION_DEFAULT),
    }
    row = (
        db.query(HistoricalSetupFeature)
        .filter(HistoricalSetupFeature.symbol == example.symbol)
        .filter(HistoricalSetupFeature.interval == example.interval)
        .filter(HistoricalSetupFeature.timestamp == example.timestamp)
        .filter(HistoricalSetupFeature.feature_version == version)
        .first()
    )
    if row:
        row.setup_family = example.setup_family
        row.direction = example.direction
        row.setup_state = example.setup_state
        row.data_quality = example.data_quality
        row.features_json = json.dumps(payload, sort_keys=True)
        row.updated_at = now_iso()
        return row
    row = HistoricalSetupFeature(
        symbol=example.symbol,
        interval=example.interval,
        timestamp=example.timestamp,
        feature_version=version,
        setup_family=example.setup_family,
        direction=example.direction,
        setup_state=example.setup_state,
        data_quality=example.data_quality,
        features_json=json.dumps(payload, sort_keys=True),
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(row)
    return row


def _persist_outcome(db: Session, feature: HistoricalSetupFeature, horizon: str, outcome: dict[str, Any]) -> None:
    version = str(_cfg().get("outcome_version", OUTCOME_VERSION_DEFAULT) or OUTCOME_VERSION_DEFAULT)
    db.flush()
    existing = (
        db.query(HistoricalSetupOutcome)
        .filter(HistoricalSetupOutcome.feature_id == feature.id)
        .filter(HistoricalSetupOutcome.outcome_version == version)
        .filter(HistoricalSetupOutcome.horizon == horizon)
        .first()
    )
    values = {
        "forward_return_pct": _safe_float(outcome.get("forward_return_pct")),
        "mfe_pct": _safe_float(outcome.get("mfe_pct")),
        "mae_pct": _safe_float(outcome.get("mae_pct")),
        "time_to_mfe_minutes": int(outcome.get("time_to_mfe_minutes") or 0),
        "time_to_mae_minutes": int(outcome.get("time_to_mae_minutes") or 0),
        "target_1_reached": bool(outcome.get("target_1_reached")),
        "target_2_reached": bool(outcome.get("target_2_reached")),
        "invalidation_reached": bool(outcome.get("invalidation_reached")),
        "target_1_before_invalidation": bool(outcome.get("target_1_before_invalidation")),
        "target_2_before_invalidation": bool(outcome.get("target_2_before_invalidation")),
        "invalidation_before_target": bool(outcome.get("invalidation_before_target")),
        "directional_outcome": outcome.get("directional_outcome"),
        "profitable_after_costs": bool(outcome.get("profitable_after_costs")),
        "outcome_json": json.dumps(outcome, sort_keys=True),
        "updated_at": now_iso(),
    }
    if existing:
        for key, value in values.items():
            setattr(existing, key, value)
        return
    db.add(
        HistoricalSetupOutcome(
            feature_id=feature.id,
            outcome_version=version,
            horizon=horizon,
            created_at=now_iso(),
            **values,
        )
    )


def _examples_for_symbol(symbol: str, interval: str, period: str, db: Session, spy_frame: pd.DataFrame, qqq_frame: pd.DataFrame) -> tuple[pd.DataFrame, list[FeatureExample]]:
    frame = _load_enriched(symbol, interval, period, db)
    if frame.empty or len(frame) < 80:
        return frame, []
    frame.attrs["interval"] = interval
    spy_rel = _benchmark_relative(frame, spy_frame)
    qqq_rel = _benchmark_relative(frame, qqq_frame)
    examples: list[FeatureExample] = []
    for pos in range(55, len(frame)):
        example = _feature_from_row(symbol, interval, frame, pos, spy_rel, qqq_rel)
        if example:
            examples.append(example)
    return frame, examples


def _match_record(current: FeatureExample, candidate: FeatureExample, frame: pd.DataFrame, horizon: str = "2h") -> dict[str, Any] | None:
    outcome = _outcome_for(frame, candidate.index_position, current.direction, HORIZON_BARS[horizon])
    if not outcome:
        return None
    current_flags = _indicator_flags(current, current.direction)
    candidate_flags = _indicator_flags(candidate, current.direction)
    matching = [key for key, value in current_flags.items() if value and candidate_flags.get(key)]
    conflicting = [key for key, value in current_flags.items() if value and not candidate_flags.get(key)]
    return {
        "symbol": candidate.symbol,
        "timestamp": _iso_from_ts(candidate.timestamp),
        "setup_family": candidate.setup_family,
        "direction": candidate.direction,
        "market_regime": "unavailable",
        "catalyst_state": "unavailable",
        "similarity_score": _similarity(current, candidate),
        "matching_indicators": matching,
        "conflicting_indicators": conflicting,
        "forward_outcome": outcome.get("directional_outcome"),
        "forward_return_pct": round(float(outcome.get("forward_return_pct") or 0.0), 3),
        "mfe_pct": round(float(outcome.get("mfe_pct") or 0.0), 3),
        "mae_pct": round(float(outcome.get("mae_pct") or 0.0), 3),
        "time_to_mfe_minutes": outcome.get("time_to_mfe_minutes"),
        "time_to_mae_minutes": outcome.get("time_to_mae_minutes"),
        "target_1_before_invalidation": bool(outcome.get("target_1_before_invalidation")),
        "target_2_before_invalidation": bool(outcome.get("target_2_before_invalidation")),
        "invalidation_before_target": bool(outcome.get("invalidation_before_target")),
        "profitable_after_costs": bool(outcome.get("profitable_after_costs")),
        "data_quality": candidate.data_quality,
        "outcome": outcome,
    }


def _dedupe_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gap_bars = int(_cfg().get("event_gap_bars", 8) or 8)
    seen: dict[tuple[str, str], int] = {}
    deduped: list[dict[str, Any]] = []
    interval = interval_seconds(str(_cfg().get("interval", "15m") or "15m"))
    event_gap_seconds = gap_bars * interval
    for match in sorted(matches, key=lambda row: (row["symbol"], row["setup_family"], row["timestamp"])):
        ts = int(datetime.fromisoformat(str(match["timestamp"]).replace("Z", "+00:00")).timestamp())
        key = (match["symbol"], match["setup_family"])
        previous = seen.get(key)
        if previous is not None and ts - previous < event_gap_seconds:
            continue
        seen[key] = ts
        deduped.append(match)
    return sorted(deduped, key=lambda row: row["similarity_score"], reverse=True)


def _summarize_scope(matches: list[dict[str, Any]], *, scope: str) -> dict[str, Any]:
    matches = list(matches)
    n = len(matches)
    if n <= 0:
        return {
            "scope": scope,
            "examples": 0,
            "successes": 0,
            "raw_success_rate": None,
            "confidence_interval": {"low": None, "high": None},
            "out_of_sample_success_rate": None,
            "calibration": {"brier_score": None, "calibration_error": None, "status": "INSUFFICIENT DATA"},
            "confidence": "INSUFFICIENT",
            "average_return_pct": None,
            "median_return_pct": None,
            "average_mfe_pct": None,
            "average_mae_pct": None,
            "worst_outcome_pct": None,
            "expected_value_pct": None,
            "probabilities": {},
            "warning": "Fewer than 10 comparable examples; no reliable probability should be inferred.",
        }
    successes = sum(1 for m in matches if m.get("target_1_before_invalidation"))
    hit_rate = successes / n
    returns = [float(m.get("forward_return_pct") or 0.0) for m in matches]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = abs(float(np.mean(losses))) if losses else 0.0
    cost_pct = float(_cfg().get("modeled_cost_pct", 0.4) or 0.4)
    expected_value = (hit_rate * avg_win) - ((1 - hit_rate) * avg_loss) - cost_pct

    chronological = sorted(matches, key=lambda row: row["timestamp"])
    validation_count = max(0, int(len(chronological) * 0.3))
    validation = chronological[-validation_count:] if validation_count >= 5 else []
    oos = (sum(1 for m in validation if m.get("target_1_before_invalidation")) / len(validation)) if validation else None
    brier = None
    calibration_error = None
    if validation:
        ys = np.array([1.0 if m.get("target_1_before_invalidation") else 0.0 for m in validation])
        p = np.full_like(ys, hit_rate, dtype=float)
        brier = float(np.mean((p - ys) ** 2))
        calibration_error = abs(float(np.mean(ys)) - hit_rate)

    bullish = sum(1 for m in matches if m.get("forward_outcome") == "BULLISH")
    bearish = sum(1 for m in matches if m.get("forward_outcome") == "BEARISH")
    neutral = n - bullish - bearish
    profitable = sum(1 for m in matches if m.get("profitable_after_costs"))
    target2 = sum(1 for m in matches if m.get("target_2_before_invalidation"))
    invalidation = sum(1 for m in matches if m.get("invalidation_before_target"))
    ci = wilson_interval(successes, n)
    confidence = _confidence_from_sample(n)
    if confidence != "INSUFFICIENT" and ci["low"] is not None and ci["high"] is not None and (ci["high"] - ci["low"]) > 0.35:
        confidence = "LOW"
    if oos is not None and abs(oos - hit_rate) > 0.18 and confidence in {"MODERATE", "STRONG"}:
        confidence = "LOW"

    return {
        "scope": scope,
        "examples": n,
        "successes": successes,
        "raw_success_rate": round(hit_rate, 4),
        "confidence_interval": ci,
        "out_of_sample_success_rate": round(oos, 4) if oos is not None else None,
        "calibration": {
            "brier_score": round(brier, 4) if brier is not None else None,
            "calibration_error": round(calibration_error, 4) if calibration_error is not None else None,
            "status": "INSUFFICIENT DATA" if brier is None else "CALIBRATED SAMPLE CHECK",
        },
        "confidence": confidence,
        "average_return_pct": round(float(np.mean(returns)), 3),
        "median_return_pct": round(float(np.median(returns)), 3),
        "average_mfe_pct": round(float(np.mean([m.get("mfe_pct") or 0.0 for m in matches])), 3),
        "average_mae_pct": round(float(np.mean([m.get("mae_pct") or 0.0 for m in matches])), 3),
        "worst_outcome_pct": round(float(np.min(returns)), 3),
        "expected_value_pct": round(float(expected_value), 3),
        "probabilities": {
            "bullish_move": {"successes": bullish, "examples": n, "rate": round(bullish / n, 4)},
            "bearish_move": {"successes": bearish, "examples": n, "rate": round(bearish / n, 4)},
            "neutral_outcome": {"successes": neutral, "examples": n, "rate": round(neutral / n, 4)},
            "target_1_before_invalidation": {"successes": successes, "examples": n, "rate": round(hit_rate, 4)},
            "target_2_before_invalidation": {"successes": target2, "examples": n, "rate": round(target2 / n, 4)},
            "invalidation_before_target": {"successes": invalidation, "examples": n, "rate": round(invalidation / n, 4)},
            "profitable_after_modeled_costs": {"successes": profitable, "examples": n, "rate": round(profitable / n, 4)},
        },
        "warning": (
            "Fewer than 10 comparable examples; no reliable probability should be inferred."
            if n < int(_cfg().get("min_examples", 10) or 10)
            else "Sample size is small; confidence interval is wide."
            if n < int(_cfg().get("moderate_examples", 30) or 30)
            else None
        ),
    }


def _latest_positioning(symbol: str, db: Session) -> dict[str, Any] | None:
    row = (
        db.query(OptionPositioningSnapshot)
        .filter(OptionPositioningSnapshot.symbol == symbol)
        .order_by(desc(OptionPositioningSnapshot.created_at))
        .first()
    )
    if not row:
        return None
    try:
        payload = json.loads(row.positioning_json)
    except Exception:
        payload = {}
    return {
        "classification": row.classification,
        "bias_score": row.bias_score,
        "provider": row.provider,
        "session_state": row.session_state,
        "created_at": row.created_at,
        "data_status": "observed",
        "snapshot": payload,
    }


def _contract_score(contract: dict[str, Any], side: str, expected_value_pct: float | None) -> tuple[float, list[str], list[str]]:
    options_cfg = config_manager.get("options", default={}) or {}
    warnings: list[str] = []
    blockers: list[str] = []
    score = 100.0
    bid = _safe_float(contract.get("bid"), 0.0) or 0.0
    ask = _safe_float(contract.get("ask"), 0.0) or 0.0
    spread = _safe_float(contract.get("spread_percentage"), _safe_float(contract.get("spread_pct"))) or 0.0
    volume = int(_safe_float(contract.get("volume"), 0.0) or 0)
    oi = int(_safe_float(contract.get("open_interest"), 0.0) or 0)
    delta = _safe_float(contract.get("delta"))
    theta = _safe_float(contract.get("theta"))
    quote_stale = bool(contract.get("quote_stale"))
    if bid <= 0:
        blockers.append("no bid")
        score -= 40
    if ask <= 0:
        blockers.append("no ask")
        score -= 25
    max_spread = float(options_cfg.get("max_spread_pct", 15) or 15)
    if spread > max_spread:
        blockers.append(f"spread {spread:.2f}% above max {max_spread:.2f}%")
        score -= 30
    elif spread > float(options_cfg.get("recommended_max_spread_pct", 5) or 5):
        warnings.append(f"spread {spread:.2f}% is above preferred max")
        score -= 10
    if volume < int(options_cfg.get("min_volume", 50) or 50):
        warnings.append("low option volume")
        score -= 12
    if oi < int(options_cfg.get("min_open_interest", 100) or 100):
        warnings.append("low open interest")
        score -= 12
    if quote_stale:
        blockers.append("stale quote")
        score -= 25
    if delta is not None:
        abs_delta = abs(delta)
        if abs_delta < 0.3 or abs_delta > 0.85:
            warnings.append("delta is outside preferred directional range")
            score -= 8
    else:
        warnings.append("delta unavailable")
        score -= 6
    if theta is not None and ask > 0 and abs(theta) / ask > 0.12:
        warnings.append("theta burden is high relative to premium")
        score -= 10
    if expected_value_pct is not None and expected_value_pct <= 0:
        warnings.append("historical expected value is not positive")
        score -= 12
    return max(0.0, score), blockers, warnings


def _contract_selection(symbol: str, side: str, expected_value_pct: float | None) -> dict[str, Any]:
    if not bool(_cfg().get("include_contract_selection", True)):
        return {"status": "NOT_REQUESTED", "message": "Contract selection disabled in configuration."}
    options_cfg = config_manager.get("options", default={}) or {}
    try:
        payload = ranked_contracts(
            symbol,
            expirations_to_check=int(options_cfg.get("expirations_to_check", 3) or 3),
            min_volume=1,
            max_spread_pct=float(options_cfg.get("max_spread_pct", 15) or 15),
            min_open_interest=1,
        )
    except Exception as exc:
        return {"status": "UNAVAILABLE", "message": str(exc)}
    book = payload.get("calls" if side == "LONG" else "puts") or []
    reviewed = []
    for contract in book[:12]:
        score, blockers, warnings = _contract_score(contract, side, expected_value_pct)
        reviewed.append(
            {
                "contract": contract.get("contract_symbol"),
                "type": contract.get("type"),
                "expiration": contract.get("expiration"),
                "strike": contract.get("strike"),
                "bid": contract.get("bid"),
                "ask": contract.get("ask"),
                "midpoint": contract.get("midpoint"),
                "spread_pct": contract.get("spread_percentage") or contract.get("spread_pct"),
                "volume": contract.get("volume"),
                "open_interest": contract.get("open_interest"),
                "delta": contract.get("delta"),
                "gamma": contract.get("gamma"),
                "theta": contract.get("theta"),
                "vega": contract.get("vega"),
                "implied_volatility": contract.get("implied_volatility"),
                "quality_score": round(score, 2),
                "blockers": blockers,
                "warnings": warnings,
                "acceptable": not blockers and score >= 60,
                "why": "Rejected by hard gates." if blockers else "Acceptable deterministic contract candidate.",
            }
        )
    acceptable = [row for row in reviewed if row["acceptable"]]
    acceptable.sort(key=lambda row: row["quality_score"], reverse=True)
    if not acceptable:
        return {
            "status": "NO_ACCEPTABLE_CONTRACT",
            "provider": payload.get("provider") or payload.get("source"),
            "timestamp": payload.get("timestamp"),
            "reviewed": reviewed,
            "message": "No contract passed no-bid, spread, freshness, liquidity, and structural quality gates.",
        }
    return {
        "status": "OK",
        "provider": payload.get("provider") or payload.get("source"),
        "timestamp": payload.get("timestamp"),
        "best_contract": acceptable[0],
        "safer_contract": next((row for row in acceptable if abs(_safe_float(row.get("delta"), 0.0) or 0.0) >= 0.55), acceptable[0]),
        "higher_leverage_contract": acceptable[-1],
        "reviewed": reviewed,
        "message": "Selected from cached/rate-limited ranked contracts. Historical probability did not override liquidity gates.",
    }


def _persist_family(db: Session, current: FeatureExample, summary: dict[str, Any]) -> None:
    version = str(_cfg().get("feature_version", FEATURE_VERSION_DEFAULT) or FEATURE_VERSION_DEFAULT)
    definition = {
        "qualifying_indicators": [key for key, value in _indicator_flags(current, current.direction).items() if value],
        "disqualifying_indicators": [key for key, value in _indicator_flags(current, current.direction).items() if not value],
        "matching_algorithm": str(_cfg().get("matching_version", MATCHING_VERSION_DEFAULT) or MATCHING_VERSION_DEFAULT),
    }
    row = (
        db.query(HistoricalSetupFamily)
        .filter(HistoricalSetupFamily.setup_name == current.setup_family)
        .filter(HistoricalSetupFamily.setup_version == version)
        .first()
    )
    values = {
        "direction": current.direction,
        "definition_json": json.dumps(definition, sort_keys=True),
        "stats_json": json.dumps(summary, sort_keys=True),
        "sample_size": int(summary.get("examples") or 0),
        "confidence": summary.get("confidence"),
        "last_recalculated_at": now_iso(),
        "updated_at": now_iso(),
    }
    if row:
        for key, value in values.items():
            setattr(row, key, value)
        return
    db.add(
        HistoricalSetupFamily(
            setup_name=current.setup_family,
            setup_version=version,
            created_at=now_iso(),
            **values,
        )
    )


def _watchlist_symbols(db: Session, symbol: str) -> list[str]:
    max_symbols = int(_cfg().get("max_cross_symbols", 50) or 50)
    rows = db.query(Watchlist).filter(Watchlist.active.is_(True)).all()
    symbols = [str(row.symbol or "").upper() for row in rows if str(row.symbol or "").upper() != symbol]
    return symbols[:max_symbols] if max_symbols > 0 else symbols


def build_historical_setup_match(
    symbol: str,
    *,
    side: str | None = None,
    interval: str | None = None,
    period: str | None = None,
    include_contracts: bool | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    cfg = _cfg()
    normalized = str(symbol or "").strip().upper()
    interval = str(interval or cfg.get("interval", "15m") or "15m")
    period = str(period or cfg.get("default_period", "3y") or "3y")
    threshold = float(cfg.get("similarity_threshold", 68) or 68)
    max_matches = int(cfg.get("max_matches_per_scope", 250) or 250)
    if include_contracts is None:
        include_contracts = bool(cfg.get("include_contract_selection", True))

    db = SessionLocal()
    try:
        spy_frame = _load_enriched("SPY", interval, period, db)
        qqq_frame = _load_enriched("QQQ", interval, period, db)
        current_frame, same_examples = _examples_for_symbol(normalized, interval, period, db, spy_frame, qqq_frame)
        if current_frame.empty or not same_examples:
            return {
                "symbol": normalized,
                "interval": interval,
                "period": period,
                "feature_version": cfg.get("feature_version", FEATURE_VERSION_DEFAULT),
                "matching_version": cfg.get("matching_version", MATCHING_VERSION_DEFAULT),
                "data_status": "INSUFFICIENT_DATA",
                "setup_state": "DATA INSUFFICIENT",
                "setup_name": None,
                "direction": str(side or "UNKNOWN").upper(),
                "message": "No stored completed 15-minute history is available yet. Queue or resume the historical backfill; the engine will not fabricate examples.",
                "backfill": {
                    "requested_period": period,
                    "recommended_intervals": cfg.get("backfill_intervals", ["15m", "1d"]),
                    "uses_existing_sql_first": True,
                },
                "same_symbol": _summarize_scope([], scope="same_symbol"),
                "cross_symbol": _summarize_scope([], scope="cross_symbol"),
                "matches": [],
            }

        current = same_examples[-1]
        if side:
            current.direction = str(side).upper()
        current_flags = _indicator_flags(current, current.direction)
        same_matches: list[dict[str, Any]] = []
        for candidate in same_examples[:-10]:
            if candidate.timestamp >= current.timestamp:
                continue
            if candidate.direction != current.direction and candidate.setup_family != current.setup_family:
                continue
            record = _match_record(current, candidate, current_frame)
            if record and record["similarity_score"] >= threshold:
                same_matches.append(record)

        same_matches = _dedupe_matches(same_matches)[:max_matches]

        cross_matches: list[dict[str, Any]] = []
        for other_symbol in _watchlist_symbols(db, normalized):
            frame, examples = _examples_for_symbol(other_symbol, interval, period, db, spy_frame, qqq_frame)
            if frame.empty:
                continue
            for candidate in examples[:-10]:
                if candidate.direction != current.direction and candidate.setup_family != current.setup_family:
                    continue
                record = _match_record(current, candidate, frame)
                if record and record["similarity_score"] >= threshold:
                    cross_matches.append(record)
        cross_matches = _dedupe_matches(cross_matches)[:max_matches]

        same_summary = _summarize_scope(same_matches, scope="same_symbol")
        cross_summary = _summarize_scope(cross_matches, scope="cross_symbol")
        primary_summary = same_summary if same_summary["examples"] >= int(cfg.get("min_examples", 10) or 10) else cross_summary
        option_positioning = _latest_positioning(normalized, db)
        expected_value = primary_summary.get("expected_value_pct")
        contract_selection = _contract_selection(normalized, current.direction, _safe_float(expected_value)) if include_contracts else {"status": "NOT_REQUESTED"}

        if persist:
            current_row = _persist_feature(db, current)
            if current_row is not None:
                for horizon, bars in HORIZON_BARS.items():
                    outcome = _outcome_for(current_frame, current.index_position, current.direction, bars)
                    if outcome:
                        _persist_outcome(db, current_row, horizon, outcome)
            for match in same_matches[:50]:
                match_ts = int(datetime.fromisoformat(str(match["timestamp"]).replace("Z", "+00:00")).timestamp())
                example = next((item for item in same_examples if item.timestamp == match_ts), None)
                if example:
                    feature_row = _persist_feature(db, example)
                    outcome = match.get("outcome")
                    if feature_row is not None and outcome:
                        _persist_outcome(db, feature_row, "2h", outcome)
            _persist_family(db, current, primary_summary)
            db.commit()

        confidence = primary_summary.get("confidence") or "INSUFFICIENT"
        status = current.setup_state
        if confidence == "INSUFFICIENT":
            status = "DATA INSUFFICIENT"
        elif (primary_summary.get("expected_value_pct") or 0) <= 0:
            status = "WEAKENING"
        elif current.setup_state == "CONFIRMING":
            status = "CONFIRMING"
        elif current.setup_state == "DATA INSUFFICIENT":
            status = "DATA INSUFFICIENT"

        top_matches = sorted(same_matches + cross_matches, key=lambda row: row["similarity_score"], reverse=True)[:8]
        return {
            "symbol": normalized,
            "interval": interval,
            "period": period,
            "feature_version": cfg.get("feature_version", FEATURE_VERSION_DEFAULT),
            "outcome_version": cfg.get("outcome_version", OUTCOME_VERSION_DEFAULT),
            "matching_version": cfg.get("matching_version", MATCHING_VERSION_DEFAULT),
            "timestamp": _iso_from_ts(current.timestamp),
            "data_status": current.features.get("data_status"),
            "setup_name": current.setup_family,
            "setup_state": status,
            "direction": current.direction,
            "similarity_threshold": threshold,
            "exact_rule_match": {
                "method": "deterministic indicator flags",
                "matched": all(value for value in current_flags.values()),
                "qualifying_indicators": [key for key, value in current_flags.items() if value],
                "missing_indicators": [key for key, value in current_flags.items() if not value],
            },
            "current_feature_vector": {
                "features": current.features,
                "vector": current.vector,
                "matching_indicators": [key for key, value in current_flags.items() if value],
                "missing_or_unconfirmed": [key for key, value in current_flags.items() if not value],
            },
            "same_symbol": same_summary,
            "cross_symbol": cross_summary,
            "primary_scope": primary_summary.get("scope"),
            "estimated_probability": {
                "target_1_before_invalidation": primary_summary.get("raw_success_rate"),
                "confidence": confidence,
                "language": _probability_language(primary_summary),
            },
            "current_confirmation": {
                "volume": "confirmed" if (current.features.get("relative_volume") or 0) >= 1.2 else "not confirmed",
                "options_positioning": option_positioning or {"data_status": "unavailable"},
                "greek_risk": "evaluated in contract selection" if contract_selection.get("status") == "OK" else "unavailable",
                "news_impact": "use News and Catalyst Impact panel; not included in deterministic probability unless stored as features",
            },
            "confirmation_condition": _confirmation_condition(current),
            "invalidation_condition": _invalidation_condition(current),
            "contract_selection": contract_selection,
            "matches": [
                {key: value for key, value in match.items() if key != "outcome"}
                for match in top_matches
            ],
            "historical_chart_overlay": {
                "available": bool(top_matches),
                "note": "Closest matches include setup timestamp and feature comparison. Future candles are omitted until a detail view requests outcome data.",
            },
            "backfill": {
                "requested_period": period,
                "recommended_intervals": cfg.get("backfill_intervals", ["15m", "1d"]),
                "uses_existing_sql_first": True,
                "provider_safe": "Backfill runs through existing chunked, throttled, resumable history worker.",
            },
            "warnings": _result_warnings(same_summary, cross_summary, contract_selection),
        }
    finally:
        db.close()


def _probability_language(summary: dict[str, Any]) -> str:
    n = int(summary.get("examples") or 0)
    successes = int(summary.get("successes") or 0)
    rate = summary.get("raw_success_rate")
    confidence = summary.get("confidence") or "INSUFFICIENT"
    if n < int(_cfg().get("min_examples", 10) or 10) or rate is None:
        return "Insufficient historical evidence. Fewer than 10 comparable examples passed the similarity and deduplication filters."
    return (
        f"Historically, {successes} of {n} comparable setups reached Target 1 before invalidation. "
        f"Raw hit rate: {rate * 100:.1f}%. Confidence: {confidence.title()}."
    )


def _confirmation_condition(current: FeatureExample) -> dict[str, Any]:
    price = float(current.features.get("price") or 0.0)
    resistance = _safe_float(current.features.get("resistance"))
    support = _safe_float(current.features.get("support"))
    rel_volume = max(1.2, float(_cfg().get("confirmation_relative_volume", 1.3) or 1.3))
    if current.direction == "SHORT":
        trigger = support if support and support > 0 else price
        return {
            "price": round(trigger, 2),
            "condition": f"Use only after a completed 15-minute close below {trigger:.2f} with relative volume above {rel_volume:.1f}.",
        }
    trigger = resistance if resistance and resistance > 0 else price
    return {
        "price": round(trigger, 2),
        "condition": f"Use only after a completed 15-minute close above {trigger:.2f} with relative volume above {rel_volume:.1f}.",
    }


def _invalidation_condition(current: FeatureExample) -> dict[str, Any]:
    price = float(current.features.get("price") or 0.0)
    atr = float(current.features.get("atr") or 0.0)
    vwap = _safe_float(current.features.get("vwap"))
    if current.direction == "SHORT":
        invalidation = max(vwap or price, price + (atr * float(_cfg().get("invalidation_atr", 0.75) or 0.75)))
        return {"price": round(invalidation, 2), "condition": f"Setup weakens on a completed 15-minute reclaim above {invalidation:.2f}."}
    invalidation = min(vwap or price, price - (atr * float(_cfg().get("invalidation_atr", 0.75) or 0.75)))
    return {"price": round(invalidation, 2), "condition": f"Setup weakens on a completed 15-minute close below {invalidation:.2f}."}


def _result_warnings(same_summary: dict[str, Any], cross_summary: dict[str, Any], contract_selection: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if same_summary.get("confidence") == "INSUFFICIENT":
        warnings.append("Same-symbol sample is insufficient; broader-universe data is shown separately and should not be silently combined.")
    if cross_summary.get("confidence") == "INSUFFICIENT":
        warnings.append("Cross-symbol sample is insufficient after similarity and event-deduplication filters.")
    if contract_selection.get("status") == "NO_ACCEPTABLE_CONTRACT":
        warnings.append("No option contract currently passes deterministic liquidity and structure gates.")
    if contract_selection.get("status") == "UNAVAILABLE":
        warnings.append(f"Contract selection unavailable: {contract_selection.get('message')}")
    return warnings
