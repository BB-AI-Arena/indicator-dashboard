from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    config_loaded: bool
    config_error: str | None = None


class WatchlistAddRequest(BaseModel):
    symbol: str


class Candle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class SignalResult(BaseModel):
    symbol: str
    price: float
    side: str
    score: int
    max_score: int
    grade: str
    reasons: list[str]
    warnings: list[str]
    timestamp: str
    indicators: dict[str, Any]
    option_ratios: dict[str, Any] | None = None
    alert: bool = False
