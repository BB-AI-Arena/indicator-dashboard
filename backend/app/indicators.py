from __future__ import annotations

import numpy as np
import pandas as pd


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def apply_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = df.copy()

    ema_fast = int(cfg.get("ema_fast", 8))
    ema_slow = int(cfg.get("ema_slow", 21))
    ema_trend = int(cfg.get("ema_trend", 50))
    rsi_period = int(cfg.get("rsi_period", 14))
    atr_period = int(cfg.get("atr_period", 14))
    bb_period = int(cfg.get("bollinger_period", 20))
    bb_std = float(cfg.get("bollinger_std", 2))
    vol_avg_period = int(cfg.get("volume_avg_period", 20))
    macd_fast = int(cfg.get("macd_fast", 12))
    macd_slow = int(cfg.get("macd_slow", 26))
    macd_signal = int(cfg.get("macd_signal", 9))

    out["ema_fast"] = out["close"].ewm(span=ema_fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=ema_slow, adjust=False).mean()
    out["ema_trend"] = out["close"].ewm(span=ema_trend, adjust=False).mean()

    typical_price = (out["high"] + out["low"] + out["close"]) / 3
    cumulative_vp = (typical_price * out["volume"]).cumsum()
    cumulative_vol = out["volume"].replace(0, np.nan).cumsum()
    out["vwap"] = cumulative_vp / cumulative_vol

    out["rsi"] = _rsi(out["close"], rsi_period)

    macd_fast_ema = out["close"].ewm(span=macd_fast, adjust=False).mean()
    macd_slow_ema = out["close"].ewm(span=macd_slow, adjust=False).mean()
    out["macd_line"] = macd_fast_ema - macd_slow_ema
    out["macd_signal"] = out["macd_line"].ewm(span=macd_signal, adjust=False).mean()
    out["macd_hist"] = out["macd_line"] - out["macd_signal"]

    out["bb_mid"] = out["close"].rolling(bb_period, min_periods=bb_period).mean()
    bb_sigma = out["close"].rolling(bb_period, min_periods=bb_period).std(ddof=0)
    out["bb_upper"] = out["bb_mid"] + (bb_sigma * bb_std)
    out["bb_lower"] = out["bb_mid"] - (bb_sigma * bb_std)

    out["atr"] = _atr(out, atr_period)
    out["volume_avg"] = out["volume"].rolling(vol_avg_period, min_periods=1).mean()
    out["volume_spike"] = out["volume"] > out["volume_avg"]

    out = out.replace([np.inf, -np.inf], np.nan)
    return out
