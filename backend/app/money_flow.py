from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .providers.option_positioning import alignment_score_for_side


EASTERN = ZoneInfo("America/New_York")


def _safe_float(value: Any, fallback: float | None = None) -> float | None:
    try:
        if value is None:
            return fallback
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return fallback
        return number
    except Exception:
        return fallback


def _safe_int(value: Any, fallback: int | None = None) -> int | None:
    try:
        if value is None:
            return fallback
        return int(float(value))
    except Exception:
        return fallback


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _safe_text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_et(value: Any) -> datetime | None:
    parsed = _parse_timestamp(value)
    if not parsed:
        return None
    return parsed.astimezone(EASTERN)


def _normalize_candles(candles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in candles or []:
        time_value = _safe_int(row.get("time"))
        open_ = _safe_float(row.get("open"))
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        close = _safe_float(row.get("close"))
        volume = _safe_float(row.get("volume"), 0.0)
        if time_value is None or open_ is None or high is None or low is None or close is None:
            continue
        rows.append(
            {
                "time": time_value,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume or 0.0,
            }
        )
    return sorted(rows, key=lambda row: row["time"])


def _latest_indicator_row(indicator_data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(indicator_data, dict):
        return {}
    latest = indicator_data.get("latest")
    if isinstance(latest, dict):
        return latest
    indicators = indicator_data.get("indicators")
    if isinstance(indicators, list) and indicators:
        return indicators[-1] if isinstance(indicators[-1], dict) else {}
    return {}


def _previous_indicator_row(indicator_data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(indicator_data, dict):
        return {}
    indicators = indicator_data.get("indicators")
    if isinstance(indicators, list) and len(indicators) >= 2:
        row = indicators[-2]
        if isinstance(row, dict):
            return row
    return {}


def _fallback_vwap(candles: list[dict[str, Any]], upto_index: int | None = None) -> float | None:
    rows = candles if upto_index is None else candles[: max(0, upto_index)]
    if not rows:
        return None
    total_volume = sum(float(row.get("volume") or 0.0) for row in rows)
    if total_volume <= 0:
        return None
    total_value = sum(float(row.get("close") or 0.0) * float(row.get("volume") or 0.0) for row in rows)
    return round(total_value / total_volume, 4)


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    num = _safe_float(numerator)
    den = _safe_float(denominator)
    if num is None or den in (None, 0):
        return None
    return round(num / den, 4)


def _percentage(part: float | int | None, total: float | int | None) -> float | None:
    num = _safe_float(part)
    den = _safe_float(total)
    if num is None or den in (None, 0):
        return None
    return round((num / den) * 100.0, 2)


def _bucket_time(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(EASTERN)
    return dt.strftime("%H:%M")


def _session_label(session: dict[str, Any] | None) -> tuple[str, str, bool]:
    session = session or {}
    state = _safe_text(session.get("session_state")).upper() or "UNKNOWN"
    actionable = bool(session.get("actionable_live_quotes", True))
    if actionable and state in {"REGULAR", "EARLY_CLOSE"}:
        return state, "Live", True
    if state in {"PREMARKET", "AFTER_HOURS", "MARKET_CLOSED", "HOLIDAY"}:
        return state, "Previous session", False
    return state, "Live" if actionable else "Previous session", actionable


def _direction_sign(side: str | None) -> int:
    normalized = _safe_text(side).upper()
    if normalized == "SHORT":
        return -1
    return 1


def _line_slope(values: list[float | None]) -> float | None:
    seq = [v for v in values if v is not None]
    if len(seq) < 2:
        return None
    return round(seq[-1] - seq[0], 4)


def _volume_by_session_slot(candles: list[dict[str, Any]], latest_time: int) -> dict[str, Any]:
    if not candles:
        return {"status": "unavailable", "reason": "No candles"}
    latest_dt = datetime.fromtimestamp(latest_time, tz=timezone.utc).astimezone(EASTERN)
    slot = latest_dt.strftime("%H:%M")
    same_slot = []
    for row in candles[:-1]:
        row_slot = _bucket_time(row["time"])
        if row_slot == slot:
            same_slot.append(float(row.get("volume") or 0.0))
    if len(same_slot) < 2:
        return {"status": "unavailable", "reason": "Not enough same-time-of-day history"}
    average = mean(same_slot)
    latest_volume = float(candles[-1].get("volume") or 0.0)
    return {
        "status": "observed",
        "slot": slot,
        "average_volume": round(average, 2),
        "latest_volume": round(latest_volume, 2),
        "ratio": _ratio(latest_volume, average),
    }


def _obv(candles: list[dict[str, Any]]) -> list[float]:
    values = []
    running = 0.0
    prev_close = None
    for row in candles:
        close = row["close"]
        volume = float(row.get("volume") or 0.0)
        if prev_close is not None:
            if close > prev_close:
                running += volume
            elif close < prev_close:
                running -= volume
        values.append(round(running, 2))
        prev_close = close
    return values


def _adl(candles: list[dict[str, Any]]) -> list[float]:
    values = []
    running = 0.0
    for row in candles:
        high = row["high"]
        low = row["low"]
        close = row["close"]
        volume = float(row.get("volume") or 0.0)
        if high == low:
            multiplier = 0.0
        else:
            multiplier = (((close - low) - (high - close)) / (high - low))
        running += multiplier * volume
        values.append(round(running, 2))
    return values


def _cmf(candles: list[dict[str, Any]], period: int = 20) -> float | None:
    rows = candles[-period:]
    if len(rows) < 2:
        return None
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        high = row["high"]
        low = row["low"]
        close = row["close"]
        volume = float(row.get("volume") or 0.0)
        if high == low:
            multiplier = 0.0
        else:
            multiplier = (((close - low) - (high - close)) / (high - low))
        numerator += multiplier * volume
        denominator += volume
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _mfi(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    rows = candles[-(period + 1):]
    if len(rows) < 3:
        return None
    positive = 0.0
    negative = 0.0
    previous_typical = None
    for row in rows:
        typical = (row["high"] + row["low"] + row["close"]) / 3.0
        money_flow = typical * float(row.get("volume") or 0.0)
        if previous_typical is not None:
            if typical > previous_typical:
                positive += money_flow
            elif typical < previous_typical:
                negative += money_flow
        previous_typical = typical
    if positive <= 0 and negative <= 0:
        return None
    if negative <= 0:
        return 100.0
    ratio = positive / negative
    return round(100 - (100 / (1 + ratio)), 2)


def _close_location(row: dict[str, Any]) -> float | None:
    high = _safe_float(row.get("high"))
    low = _safe_float(row.get("low"))
    close = _safe_float(row.get("close"))
    if high is None or low is None or close is None or high == low:
        return None
    return round(((close - low) / (high - low)) * 100.0, 2)


def _price_volume_section(candles: list[dict[str, Any]], latest_row: dict[str, Any], prev_row: dict[str, Any], same_slot: dict[str, Any]) -> dict[str, Any]:
    latest_close = _safe_float(latest_row.get("close"))
    prev_close = _safe_float(prev_row.get("close"))
    latest_volume = _safe_float(latest_row.get("volume"), 0.0) or 0.0
    prev_volume = _safe_float(prev_row.get("volume"), 0.0) or 0.0
    price_change = None if latest_close is None or prev_close is None else round(latest_close - prev_close, 4)
    price_change_pct = None if latest_close is None or prev_close in (None, 0) else round(((latest_close - prev_close) / prev_close) * 100.0, 2)
    volume_avg = _safe_float(latest_row.get("volume_avg"))
    relative_volume = _ratio(latest_volume, volume_avg) if volume_avg else None
    dollar_volume = None if latest_close is None else round(latest_close * latest_volume, 2)
    price_per_1k = None if price_change is None or latest_volume <= 0 else round((price_change / latest_volume) * 1000.0, 4)
    up_volume = sum(float(row.get("volume") or 0.0) for row in candles[-8:] if float(row.get("close") or 0.0) > float(row.get("open") or 0.0))
    down_volume = sum(float(row.get("volume") or 0.0) for row in candles[-8:] if float(row.get("close") or 0.0) < float(row.get("open") or 0.0))
    up_candle_volume_share = _percentage(up_volume, up_volume + down_volume)
    down_candle_volume_share = _percentage(down_volume, up_volume + down_volume)
    volume_vs_same_time = same_slot if same_slot.get("status") == "observed" else {"status": "unavailable", "reason": same_slot.get("reason")}
    return {
        "source": "observed" if candles else "unavailable",
        "timestamp": _safe_text(latest_row.get("timestamp")) or None,
        "data_status": "observed" if candles else "unavailable",
        "price_change": price_change,
        "price_change_pct": price_change_pct,
        "volume": int(latest_volume),
        "relative_volume": relative_volume,
        "dollar_volume": dollar_volume,
        "volume_vs_same_time_of_day": volume_vs_same_time,
        "price_movement_per_1k_shares": price_per_1k,
        "up_candle_volume_share_pct": up_candle_volume_share,
        "down_candle_volume_share_pct": down_candle_volume_share,
        "rising_volume_on_up_candles": up_volume > down_volume if up_volume or down_volume else None,
        "up_volume": round(up_volume, 2),
        "down_volume": round(down_volume, 2),
        "previous_close": prev_close,
        "latest_close": latest_close,
        "volume_change_pct": None if prev_volume <= 0 else round(((latest_volume - prev_volume) / prev_volume) * 100.0, 2),
    }


def _vwap_section(candles: list[dict[str, Any]], indicator_data: dict[str, Any] | None, latest_row: dict[str, Any], prev_row: dict[str, Any], market_session: dict[str, Any] | None) -> dict[str, Any]:
    latest_vwap = _safe_float(latest_row.get("vwap"))
    prev_vwap = _safe_float(prev_row.get("vwap"))
    latest_close = _safe_float(latest_row.get("close"))
    above_vwap = None if latest_close is None or latest_vwap is None else latest_close >= latest_vwap
    slope = None if latest_vwap is None or prev_vwap is None else round(latest_vwap - prev_vwap, 4)
    distance_pct = None if latest_close is None or latest_vwap in (None, 0) else round(((latest_close - latest_vwap) / latest_vwap) * 100.0, 2)
    holds = 0
    rejections = 0
    last_state = None
    for row in candles[-20:]:
        vwap = _safe_float(row.get("vwap"))
        close = _safe_float(row.get("close"))
        if vwap is None or close is None:
            continue
        state = close >= vwap
        if last_state is None:
            last_state = state
            continue
        if state and not last_state:
            holds += 1
        elif not state and last_state:
            rejections += 1
        last_state = state
    volume_confirmation = "unavailable"
    if candles:
        latest_volume = float(candles[-1].get("volume") or 0.0)
        volume_avg = _safe_float(latest_row.get("volume_avg"))
        if latest_volume and volume_avg:
            volume_confirmation = "confirmed" if latest_volume >= volume_avg else "weak"
    return {
        "source": "observed" if latest_vwap is not None else "unavailable",
        "timestamp": _safe_text(latest_row.get("timestamp")) or None,
        "data_status": "observed" if latest_vwap is not None else "unavailable",
        "above_vwap": above_vwap,
        "vwap_slope": slope,
        "holds": holds,
        "rejections": rejections,
        "distance_from_vwap_pct": distance_pct,
        "reclaim_or_lose_with_volume": volume_confirmation,
        "current_vwap": latest_vwap,
        "previous_vwap": prev_vwap,
        "session_state": (market_session or {}).get("session_state"),
        "actionable_live_quotes": bool((market_session or {}).get("actionable_live_quotes", True)),
    }


def _accumulation_section(candles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(candles) < 2:
        return {
            "source": "unavailable",
            "data_status": "unavailable",
            "reason": "Not enough candles",
        }
    obv = _obv(candles)
    adl = _adl(candles)
    cmf = _cmf(candles)
    mfi = _mfi(candles)
    obv_slope = _line_slope(obv[-5:])
    adl_slope = _line_slope(adl[-5:])
    close_location = _close_location(candles[-1])
    anchored_vwap = None
    session_open = candles[0]
    total_volume = sum(float(row.get("volume") or 0.0) for row in candles)
    if total_volume > 0:
        anchored_vwap = round(sum(float(row["close"]) * float(row.get("volume") or 0.0) for row in candles) / total_volume, 4)
    return {
        "source": "observed",
        "data_status": "observed",
        "obv": obv[-1] if obv else None,
        "obv_slope": obv_slope,
        "accumulation_distribution_line": adl[-1] if adl else None,
        "accumulation_distribution_slope": adl_slope,
        "chaikin_money_flow": cmf,
        "money_flow_index": mfi,
        "anchored_vwap": anchored_vwap,
        "up_volume": round(sum(float(row.get("volume") or 0.0) for row in candles if float(row.get("close") or 0.0) > float(row.get("open") or 0.0)), 2),
        "down_volume": round(sum(float(row.get("volume") or 0.0) for row in candles if float(row.get("close") or 0.0) < float(row.get("open") or 0.0)), 2),
        "close_location_within_range_pct": close_location,
        "session_open": session_open.get("open"),
    }


def _relative_strength_section(
    candles: list[dict[str, Any]],
    benchmark_data: dict[str, Any] | None,
) -> dict[str, Any]:
    if not candles or not isinstance(benchmark_data, dict):
        return {
            "source": "unavailable",
            "data_status": "unavailable",
            "reason": "Benchmark candles were not provided",
        }
    benchmark_candles = _normalize_candles(benchmark_data.get("candles"))
    if not benchmark_candles:
        return {
            "source": "unavailable",
            "data_status": "unavailable",
            "reason": "Benchmark candles were not provided",
        }
    ticker_return = _ratio(candles[-1]["close"] - candles[0]["close"], candles[0]["close"]) or 0.0
    bench_return = _ratio(benchmark_candles[-1]["close"] - benchmark_candles[0]["close"], benchmark_candles[0]["close"]) or 0.0
    spread = round((ticker_return - bench_return) * 100.0, 2)
    return {
        "source": benchmark_data.get("source") or benchmark_data.get("provider") or "observed",
        "data_status": "observed",
        "ticker_return_pct": round(ticker_return * 100.0, 2),
        "benchmark_return_pct": round(bench_return * 100.0, 2),
        "relative_strength_pct": spread,
        "outperforming": spread > 0,
        "holding_gains_while_market_falls": spread > 0 and bench_return < 0,
        "fails_to_rise_while_market_rises": spread < 0 and bench_return > 0,
    }


def _trade_pressure_section(trade_pressure: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(trade_pressure, dict):
        return {
            "source": "unavailable",
            "data_status": "unavailable",
            "reason": "Trade-level bid/ask classification was not provided",
        }
    ask_volume = _safe_float(trade_pressure.get("ask_volume"))
    bid_volume = _safe_float(trade_pressure.get("bid_volume"))
    total = (ask_volume or 0.0) + (bid_volume or 0.0)
    if total <= 0:
        return {
            "source": trade_pressure.get("source") or "unavailable",
            "data_status": "unavailable",
            "reason": "Trade-level classification missing volume",
        }
    return {
        "source": trade_pressure.get("source") or "observed",
        "data_status": "observed",
        "ask_volume": ask_volume,
        "bid_volume": bid_volume,
        "ask_volume_pct": _percentage(ask_volume, total),
        "bid_volume_pct": _percentage(bid_volume, total),
        "cumulative_volume_delta": _safe_float(trade_pressure.get("cumulative_volume_delta")),
        "rolling_volume_delta": _safe_float(trade_pressure.get("rolling_volume_delta")),
        "large_trade_imbalance": _safe_float(trade_pressure.get("large_trade_imbalance")),
        "aggressive_buy_sell_ratio": _ratio(ask_volume, bid_volume),
    }


def _order_book_section(order_book: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(order_book, dict):
        return {
            "source": "unavailable",
            "data_status": "unavailable",
            "reason": "Level II data was not provided",
        }
    return {
        "source": order_book.get("source") or "observed",
        "data_status": "observed",
        "total_displayed_bid_size": _safe_float(order_book.get("total_displayed_bid_size")),
        "total_displayed_ask_size": _safe_float(order_book.get("total_displayed_ask_size")),
        "bid_ask_depth_imbalance": _safe_float(order_book.get("bid_ask_depth_imbalance")),
        "liquidity_near_current_price": order_book.get("liquidity_near_current_price"),
        "repeated_bid_replenishment": order_book.get("repeated_bid_replenishment"),
        "repeated_offer_replenishment": order_book.get("repeated_offer_replenishment"),
        "large_resting_orders": order_book.get("large_resting_orders"),
        "rapid_order_cancellation": order_book.get("rapid_order_cancellation"),
    }


def _options_section(ratios: dict[str, Any] | None, side: str | None) -> dict[str, Any]:
    if not isinstance(ratios, dict):
        return {
            "source": "unavailable",
            "data_status": "unavailable",
            "reason": "Option chain ratios were not provided",
        }
    positioning = ratios.get("positioning") or {}
    bias_score = _safe_int(positioning.get("bias_score"), 0) or 0
    aligned_score = alignment_score_for_side(positioning, side)
    aggregate = ratios.get("aggregate") or {}
    return {
        "source": ratios.get("source") or ratios.get("provider") or "observed",
        "data_status": "observed",
        "classification": positioning.get("classification") or "Balanced",
        "bias": positioning.get("bias") or "NEUTRAL",
        "bias_score": bias_score,
        "alignment_score": aligned_score,
        "confidence": positioning.get("confidence") or "LOW",
        "session_label": positioning.get("session_label") or "Previous session",
        "session_state": positioning.get("session_state") or "UNKNOWN",
        "aggregate": aggregate,
        "selected_expiration": positioning.get("selected_expiration"),
        "baseline": positioning.get("baseline") or {},
        "notes": positioning.get("notes") or [],
        "scopes": positioning.get("scopes") or {},
    }


def _score_component(value: float | None, positive_threshold: float, negative_threshold: float, max_score: float = 25.0) -> float:
    if value is None:
        return 0.0
    if value >= positive_threshold:
        return max_score
    if value <= negative_threshold:
        return -max_score
    return round((value / max(abs(positive_threshold), abs(negative_threshold), 1e-9)) * max_score, 2)


def _classify(score: float, components: dict[str, Any], position_aligned: bool | None) -> str:
    available = sum(1 for value in components.values() if isinstance(value, dict) and value.get("data_status") == "observed")
    if available <= 1:
        return "INSUFFICIENT DATA"
    positive = score > 18
    negative = score < -18
    conflicted = False
    evidence_positive = bool(components.get("price_volume", {}).get("score", 0) > 15 or components.get("vwap", {}).get("score", 0) > 15)
    evidence_negative = bool(components.get("price_volume", {}).get("score", 0) < -15 or components.get("vwap", {}).get("score", 0) < -15)
    if evidence_positive and evidence_negative:
        conflicted = True
    if conflicted and abs(score) < 25:
        return "CONFLICTED"
    if positive and score >= 55:
        return "STRONG ACCUMULATION"
    if positive:
        return "MODERATE ACCUMULATION"
    if negative and score <= -55:
        return "STRONG DISTRIBUTION"
    if negative:
        return "MODERATE DISTRIBUTION"
    if conflicted:
        return "CONFLICTED"
    return "NEUTRAL"


def build_money_flow(
    *,
    symbol: str,
    side: str | None = None,
    market_session: dict[str, Any] | None = None,
    candles: list[dict[str, Any]] | None = None,
    indicator_data: dict[str, Any] | None = None,
    ratios: dict[str, Any] | None = None,
    benchmark_data: dict[str, Any] | None = None,
    trade_pressure: dict[str, Any] | None = None,
    order_book: dict[str, Any] | None = None,
    current_price: float | None = None,
    quote_timestamp: str | None = None,
    quote_type: str | None = None,
) -> dict[str, Any]:
    normalized_symbol = _safe_text(symbol).upper() or None
    normalized_side = _safe_text(side).upper() or None
    market_session = market_session or {}
    session_state, session_label, actionable_live_quotes = _session_label(market_session)
    candle_rows = _normalize_candles(candles)
    latest_row = _latest_indicator_row(indicator_data)
    prev_row = _previous_indicator_row(indicator_data)
    if candle_rows and not latest_row:
        latest_row = {
            "close": candle_rows[-1]["close"],
            "vwap": candle_rows[-1].get("vwap"),
            "volume": candle_rows[-1].get("volume"),
            "volume_avg": None,
            "timestamp": candle_rows[-1]["time"],
        }
    if len(candle_rows) >= 2 and not prev_row:
        prev_row = {
            "close": candle_rows[-2]["close"],
            "vwap": candle_rows[-2].get("vwap"),
            "volume": candle_rows[-2].get("volume"),
        }
    if candle_rows:
        if latest_row.get("vwap") is None:
            latest_row["vwap"] = _fallback_vwap(candle_rows)
        if prev_row.get("vwap") is None and len(candle_rows) >= 2:
            prev_row["vwap"] = _fallback_vwap(candle_rows[:-1])
        if latest_row.get("volume_avg") is None and len(candle_rows) >= 5:
            latest_row["volume_avg"] = round(mean(float(row.get("volume") or 0.0) for row in candle_rows[-5:]), 2)
    latest_close = _safe_float(latest_row.get("close"), current_price)
    latest_time = _safe_int(latest_row.get("time") or (candle_rows[-1]["time"] if candle_rows else None))
    same_slot = _volume_by_session_slot(candle_rows, latest_time) if latest_time is not None and candle_rows else {"status": "unavailable", "reason": "No intraday history"}

    price_volume = _price_volume_section(candle_rows, latest_row, prev_row, same_slot)
    vwap = _vwap_section(candle_rows, indicator_data, latest_row, prev_row, market_session)
    accumulation = _accumulation_section(candle_rows)
    relative_strength = _relative_strength_section(candle_rows, benchmark_data)
    trade_pressure_section = _trade_pressure_section(trade_pressure)
    order_book_section = _order_book_section(order_book)
    options = _options_section(ratios, normalized_side)

    price_score = None
    if price_volume.get("price_change_pct") is not None:
        price_score = _score_component(price_volume.get("price_change_pct"), 0.75, -0.75, 25.0)
        if (price_volume.get("relative_volume") or 0) >= 1.5:
            price_score += 10 if price_score > 0 else -10
        if price_volume.get("rising_volume_on_up_candles") is True and price_score > 0:
            price_score += 5
        if price_volume.get("rising_volume_on_up_candles") is False and price_score < 0:
            price_score -= 5
    vwap_score = None
    if vwap.get("above_vwap") is not None:
        vwap_score = 0.0
        vwap_score += 20 if vwap.get("above_vwap") else -20
        if vwap.get("vwap_slope") is not None:
            vwap_score += 20 if vwap.get("vwap_slope") > 0 else -20
        if vwap.get("holds") is not None:
            vwap_score += min(10, float(vwap.get("holds") or 0) * 3)
        if vwap.get("rejections") is not None:
            vwap_score -= min(10, float(vwap.get("rejections") or 0) * 3)
    rel_score = None
    if relative_strength.get("data_status") == "observed":
        rel_score = _score_component(relative_strength.get("relative_strength_pct"), 0.5, -0.5, 20.0)
    pressure_score = None
    if trade_pressure_section.get("data_status") == "observed":
        delta = _safe_float(trade_pressure_section.get("cumulative_volume_delta"))
        pressure_score = _score_component(delta, 0.0, 0.0, 15.0)
    accumulation_score = None
    if accumulation.get("data_status") == "observed":
        accumulation_score = 0.0
        if accumulation.get("obv_slope") is not None:
            accumulation_score += 5 if accumulation.get("obv_slope") > 0 else -5
        if accumulation.get("accumulation_distribution_slope") is not None:
            accumulation_score += 5 if accumulation.get("accumulation_distribution_slope") > 0 else -5
        if accumulation.get("chaikin_money_flow") is not None:
            accumulation_score += 5 if accumulation.get("chaikin_money_flow") > 0 else -5
        if accumulation.get("money_flow_index") is not None:
            mfi = float(accumulation.get("money_flow_index") or 0.0)
            if mfi >= 60:
                accumulation_score += 5
            elif mfi <= 40:
                accumulation_score -= 5
    option_score = None
    if options.get("data_status") == "observed":
        option_score = float(options.get("alignment_score") or 0.0)

    component_rows = [
        {"name": "price_volume", "weight": 25, "score": price_score, "available": price_score is not None},
        {"name": "vwap", "weight": 20, "score": vwap_score, "available": vwap_score is not None},
        {"name": "relative_strength", "weight": 20, "score": rel_score, "available": rel_score is not None},
        {"name": "trade_pressure", "weight": 15, "score": pressure_score, "available": pressure_score is not None},
        {"name": "accumulation", "weight": 10, "score": accumulation_score, "available": accumulation_score is not None},
        {"name": "options_positioning", "weight": 10, "score": option_score, "available": option_score is not None},
    ]

    total_weight = sum(row["weight"] for row in component_rows if row["available"])
    weighted_score = 0.0
    if total_weight > 0:
        for row in component_rows:
            if row["available"] and row["score"] is not None:
                weighted_score += (float(row["score"]) * row["weight"])
        weighted_score = round(weighted_score / total_weight, 2)
    else:
        weighted_score = 0.0

    # Reweight available evidence transparently when some components are unavailable.
    available_components = [row for row in component_rows if row["available"]]
    positive_evidence = sum(1 for row in available_components if (row["score"] or 0) > 0)
    negative_evidence = sum(1 for row in available_components if (row["score"] or 0) < 0)
    classification = _classify(weighted_score, {
        "price_volume": price_volume,
        "vwap": vwap,
        "relative_strength": relative_strength,
        "trade_pressure": trade_pressure_section,
        "accumulation": accumulation,
        "options_positioning": options,
    }, normalized_side is not None)

    if classification == "INSUFFICIENT DATA":
        confidence = "LOW"
    elif len(available_components) >= 4 and abs(weighted_score) >= 25:
        confidence = "HIGH"
    elif len(available_components) >= 2:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    alignment = "neutral"
    position_aligned = None
    if normalized_side in {"LONG", "SHORT"}:
        position_aligned = (weighted_score >= 10 and normalized_side == "LONG") or (weighted_score <= -10 and normalized_side == "SHORT")
        if position_aligned:
            alignment = "aligned"
        elif abs(weighted_score) < 10:
            alignment = "neutral"
        else:
            alignment = "conflicted"
    elif weighted_score > 10:
        alignment = "aligned"
    elif weighted_score < -10:
        alignment = "conflicted"

    buying_pressure: list[str] = []
    selling_pressure: list[str] = []
    conflicting: list[str] = []
    if price_score is not None:
        if price_score > 0:
            buying_pressure.append("Price is rising with supportive volume.")
        elif price_score < 0:
            selling_pressure.append("Price is falling with expanding volume.")
    if vwap.get("above_vwap") is True:
        buying_pressure.append("Price is above VWAP and/or VWAP is rising.")
    elif vwap.get("above_vwap") is False:
        selling_pressure.append("Price is below VWAP and/or VWAP is falling.")
    if relative_strength.get("data_status") == "observed":
        if relative_strength.get("outperforming"):
            buying_pressure.append("The ticker is outperforming the benchmark.")
        else:
            selling_pressure.append("The ticker is underperforming the benchmark.")
    if options.get("data_status") == "observed":
        if options.get("bias") == "CALL":
            buying_pressure.append("Options positioning leans call-heavy.")
        elif options.get("bias") == "PUT":
            selling_pressure.append("Options positioning leans put-heavy.")
        if normalized_side == "LONG" and options.get("bias") == "PUT":
            conflicting.append("Options positioning conflicts with a long bias.")
        if normalized_side == "SHORT" and options.get("bias") == "CALL":
            conflicting.append("Options positioning conflicts with a short bias.")
    if trade_pressure_section.get("data_status") == "observed":
        delta = _safe_float(trade_pressure_section.get("cumulative_volume_delta"))
        if delta is not None:
            if delta > 0:
                buying_pressure.append("Bid/ask trade pressure is positive.")
            elif delta < 0:
                selling_pressure.append("Bid/ask trade pressure is negative.")

    confirm_direction = [
        "Use a fresh 5-minute close through the trigger with volume still expanding.",
        "Require VWAP to hold in the intended direction before adding risk.",
    ]
    invalidate_direction = [
        "Treat a failed VWAP reclaim or a clean loss of the trigger level as a setup failure.",
        "Do not add if the benchmark turns sharply against the move and the ticker stops confirming.",
    ]
    if normalized_side == "LONG":
        confirm_direction.insert(0, "A long needs price to hold above VWAP and continue making higher highs.")
        invalidate_direction.insert(0, "A long fails if price loses VWAP or the recent swing low.")
    elif normalized_side == "SHORT":
        confirm_direction.insert(0, "A short needs price to stay below VWAP and continue making lower lows.")
        invalidate_direction.insert(0, "A short fails if price reclaims VWAP or the recent swing high.")

    advice = "Supports waiting for confirmation."
    if classification in {"STRONG ACCUMULATION", "MODERATE ACCUMULATION"} and normalized_side == "LONG":
        advice = "Supports holding or waiting for confirmation; do not chase if the move is already extended."
    elif classification in {"STRONG DISTRIBUTION", "MODERATE DISTRIBUTION"} and normalized_side == "SHORT":
        advice = "Supports holding or waiting for confirmation; tighten invalidation if the move is extended."
    elif classification == "CONFLICTED":
        advice = "Supports waiting or reducing risk until price and flow agree."
    elif classification == "INSUFFICIENT DATA":
        advice = "Supports waiting for a refresh before acting on the flow read."
    elif weighted_score < 0:
        advice = "Supports reducing exposure or avoiding a fresh add until price confirms."

    market_status = "PREVIOUS_SESSION" if not actionable_live_quotes else "FRESH"
    if session_state in {"PREMARKET", "AFTER_HOURS", "MARKET_CLOSED", "HOLIDAY"}:
        market_status = "PREVIOUS_SESSION"
    elif candles and latest_time is not None:
        latest_et = datetime.fromtimestamp(latest_time, tz=timezone.utc).astimezone(EASTERN)
        age_minutes = max(0, int((datetime.now(timezone.utc).astimezone(EASTERN) - latest_et).total_seconds() / 60))
        if actionable_live_quotes and age_minutes > 10:
            market_status = "STALE"

    return {
        "symbol": normalized_symbol,
        "side": normalized_side,
        "session_state": session_state,
        "session_label": session_label,
        "market_status": market_status,
        "data_freshness": market_status,
        "quote_timestamp": quote_timestamp,
        "quote_type": quote_type,
        "current_price": latest_close,
        "classification": classification,
        "score": round(weighted_score, 2),
        "confidence": confidence,
        "alignment": alignment,
        "position_aligned": position_aligned,
        "price_confirmation": {
            "price_change": price_volume.get("price_change"),
            "price_change_pct": price_volume.get("price_change_pct"),
            "volume": price_volume.get("volume"),
            "relative_volume": price_volume.get("relative_volume"),
            "dollar_volume": price_volume.get("dollar_volume"),
            "volume_vs_same_time_of_day": price_volume.get("volume_vs_same_time_of_day"),
            "price_movement_per_1k_shares": price_volume.get("price_movement_per_1k_shares"),
            "rising_volume_on_up_candles": price_volume.get("rising_volume_on_up_candles"),
            "source": price_volume.get("source"),
            "timestamp": price_volume.get("timestamp"),
            "data_status": price_volume.get("data_status"),
        },
        "volume_confirmation": {
            "up_candle_volume_share_pct": price_volume.get("up_candle_volume_share_pct"),
            "down_candle_volume_share_pct": price_volume.get("down_candle_volume_share_pct"),
            "source": price_volume.get("source"),
            "data_status": price_volume.get("data_status"),
        },
        "vwap_behavior": vwap,
        "relative_strength": relative_strength,
        "trade_pressure": trade_pressure_section,
        "order_book": order_book_section,
        "accumulation": accumulation,
        "options_alignment": options,
        "components": component_rows,
        "evidence_of_buying_pressure": buying_pressure,
        "evidence_of_selling_pressure": selling_pressure,
        "conflicting_evidence": conflicting,
        "what_would_confirm_direction": confirm_direction,
        "what_would_invalidate_direction": invalidate_direction,
        "position_advice": advice,
        "data_quality": {
            "source": "observed" if candle_rows else "unavailable",
            "timestamp": quote_timestamp or (latest_row.get("timestamp") if isinstance(latest_row, dict) else None),
            "session": session_state,
            "status": "observed" if candle_rows else "unavailable",
            "confidence": confidence,
        },
    }
