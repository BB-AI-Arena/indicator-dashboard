from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


FILTER_VERSION = 3
CENTRAL_TIME_ZONE = ZoneInfo("America/Chicago")


def central_today() -> date:
    return datetime.now(CENTRAL_TIME_ZONE).date()


def parse_expiration_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        digits = "".join(character for character in text if character.isdigit())
        if len(digits) >= 8:
            try:
                return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                return None
    return None


def current_expirations(expirations: list[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    comparison_date = today or central_today()
    current: list[dict[str, Any]] = []
    for expiration in expirations:
        expiration_value = expiration.get("date") if isinstance(expiration, dict) else expiration
        if (parse_expiration_date(expiration_value) or date.min) >= comparison_date:
            current.append(expiration if isinstance(expiration, dict) else {"date": str(expiration)})
    return current


def spread_pct(bid: float, ask: float) -> float | None:
    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return None
    return ((ask - bid) / midpoint) * 100


def filter_signature(min_volume: int, min_open_interest: int, max_spread_pct: float) -> dict[str, Any]:
    return {
        "min_volume": int(min_volume),
        "min_open_interest": int(min_open_interest),
        "max_spread_pct": float(max_spread_pct),
    }


def contract_rejection_reason(
    contract: dict[str, Any],
    *,
    today: date,
    min_volume: int,
    min_open_interest: int,
    max_spread_pct: float,
) -> str | None:
    expiration = parse_expiration_date(contract.get("expiration"))
    if expiration is None:
        return "missing_expiration"
    if expiration < today:
        return "expired"

    bid = float(contract.get("bid", 0) or 0)
    ask = float(contract.get("ask", 0) or 0)
    if bid <= 0 or ask <= 0:
        return "missing_bid_ask"

    spread = spread_pct(bid, ask)
    if spread is None:
        return "missing_spread"
    if spread > max_spread_pct:
        return "wide_spread"

    volume = int(contract.get("volume", 0) or 0)
    if volume < min_volume:
        return "low_volume"

    open_interest = int(contract.get("open_interest", 0) or 0)
    if open_interest < min_open_interest:
        return "low_open_interest"

    return None


def filter_contracts(
    contracts: list[dict[str, Any]],
    *,
    min_volume: int,
    min_open_interest: int,
    max_spread_pct: float,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    comparison_date = today or central_today()
    kept: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}

    for contract in contracts:
        reason = contract_rejection_reason(
            contract,
            today=comparison_date,
            min_volume=min_volume,
            min_open_interest=min_open_interest,
            max_spread_pct=max_spread_pct,
        )
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        kept.append(contract)

    return kept, rejected
