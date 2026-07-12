from app.etrade_positions import _public_position
from app.models import PaperRecommendation
from app.paper_portfolio import create_paper_order


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
