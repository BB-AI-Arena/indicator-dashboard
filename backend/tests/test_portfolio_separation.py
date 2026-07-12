from app.etrade_positions import _public_position
from app.models import PaperRecommendation
from app.paper_portfolio import adverse_fill_price, create_paper_order


def test_public_etrade_position_removes_account_key_and_paper_fields():
    result = _public_position({
        "account_id_key": "secret-account-key",
        "symbol": "AAPL",
        "market_value": 123.0,
        "paper_risk": {"status": "PROFIT TRAIL ACTIVE"},
    })
    assert "account_id_key" not in result
    assert "paper_risk" not in result


def test_paper_order_rejects_brokerage_identifiers():
    try:
        create_paper_order(None, {
            "symbol": "AAPL",
            "quantity": 1,
            "fill_price": 1.0,
            "etrade_order_id": "real-order-1",
        }, "admin")
    except ValueError as exc:
        assert "brokerage" in str(exc).lower()
    else:
        raise AssertionError("paper order accepted a brokerage identifier")


def test_paper_fill_is_adverse_by_five_percent_on_each_side():
    assert adverse_fill_price(1.0, "BUY_TO_OPEN") == 1.05
    assert adverse_fill_price(1.0, "SELL_TO_OPEN") == 0.95
    assert adverse_fill_price(1.0, "BUY_TO_CLOSE") == 1.05
    assert adverse_fill_price(1.0, "SELL_TO_CLOSE") == 0.95
