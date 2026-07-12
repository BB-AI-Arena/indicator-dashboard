from datetime import datetime, timezone

from app.option_estimation import (
    _baseline,
    _pricing,
    _provider_contract,
    black_scholes_price_greeks,
    refresh_interval_seconds,
)


def test_last_actual_trade_remains_baseline_when_bid_ask_are_missing():
    result = _baseline({"last": 4.33, "quote_timestamp": "2026-07-10T19:58:42Z"}, {})
    assert result["price"] == 4.33
    assert result["type"] == "LAST_ACTUAL_TRADE"


def test_black_scholes_returns_price_and_greeks():
    result = black_scholes_price_greeks(327.25, 330, 30 / 365, 0.35, "CALL")
    assert result is not None
    assert result["price"] > 0
    assert result["gamma"] > 0


def test_closed_session_pricing_is_estimated_and_non_executable():
    result = _pricing(
        {
            "symbol": "PANW",
            "option_symbol": "PANW260821C00330000",
            "expiration": "2026-08-21",
            "strike": 330,
            "option_type": "CALL",
            "last": 4.33,
            "underlying_price": 324.10,
            "latest_underlying_price": 327.25,
            "implied_volatility": 0.35,
            "_actual_quote_available": False,
        },
        {
            "session_state": "AFTER_HOURS",
            "actionable_live_quotes": False,
            "next_market_open": "2026-07-13T13:30:00+00:00",
        },
        datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc),
        {},
    )
    assert result["quote_state"] == "ESTIMATED_AFTER_HOURS"
    assert result["last_actual_option_price"] == 4.33
    assert result["estimated_next_open_value"] is not None
    assert result["assumptions"]["estimate_is_executable"] is False


def test_refresh_windows_use_eastern_daylight_time():
    assert refresh_interval_seconds(datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc)) == 180
    assert refresh_interval_seconds(datetime(2026, 1, 9, 14, 0, tzinfo=timezone.utc)) == 180
    assert refresh_interval_seconds(datetime(2026, 7, 10, 23, 0, tzinfo=timezone.utc)) == 1800


def test_unresolved_contract_is_not_replaced():
    class Provider:
        name = "etrade"

        def get_option_chain(self, symbol, expiration, **kwargs):
            return {"contracts": []}

    result = _provider_contract(
        Provider(),
        {"symbol": "SPCX", "option_symbol": "SPCX260821C00100000", "expiration": "2026-08-21", "strike": 100, "option_type": "CALL"},
    )
    assert result["status"] == "UNRESOLVED_SYMBOL"
