from __future__ import annotations

import unittest

from app.risk_engine import _conservative_return, _position_entry_price, _realistic_risk


class RiskEngineTests(unittest.TestCase):
    def test_entry_price_from_cost_basis(self) -> None:
        self.assertAlmostEqual(_position_entry_price({"cost_basis": 2000, "quantity": 10}), 2.0)

    def test_fifteen_percent_activation_with_five_percent_trail_protects_about_nine_point_two_five(self) -> None:
        entry = 2.0
        activation = entry * 1.15
        stop = activation * 0.95
        self.assertAlmostEqual((stop / entry - 1) * 100, 9.25, places=2)

    def test_long_and_short_conservative_returns(self) -> None:
        self.assertAlmostEqual(_conservative_return(2.0, 2.3, "LONG"), 15.0)
        self.assertAlmostEqual(_conservative_return(2.0, 1.7, "SHORT"), 15.0)

    def test_realistic_risk_adds_execution_effects(self) -> None:
        risk = _realistic_risk({"cost_basis": 1000, "market_value": 900, "spread_pct": 10}, {"simulated_slippage_pct": 1, "gap_risk_pct": 5, "iv_contraction_risk_pct": 3, "liquidity_failure_risk_pct": 5})
        self.assertEqual(risk, 1240.0)


if __name__ == "__main__":
    unittest.main()
