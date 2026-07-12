import unittest

from app.exit_management import build_exit_plan, detect_exit_behavior, evaluate_exit_management, is_complete_exit_plan


class ExitManagementTests(unittest.TestCase):
    def setUp(self):
        self.base = {
            "direction": "LONG",
            "entry_underlying_price": 100.0,
            "underlying_price": 103.0,
            "invalidation_price": 98.0,
            "target_1_price": 103.0,
            "target_2_price": 106.0,
            "entry_option_price": 2.0,
            "bid": 2.4,
            "management_context": {"vwap": 101.0, "ema_slow": 100.5, "support": 100.0, "completed_candle": True},
        }

    def test_pre_entry_plan_requires_structural_levels(self):
        plan = build_exit_plan(self.base)
        self.assertTrue(is_complete_exit_plan(plan))
        self.assertEqual(plan["risk_distance_underlying"], 2.0)
        self.assertEqual(plan["risk_per_contract_underlying"], 200.0)
        self.assertEqual(plan["management_timeframe"], "5m")

        incomplete = build_exit_plan({"direction": "LONG", "entry_underlying_price": 100.0})
        self.assertFalse(is_complete_exit_plan(incomplete))
        self.assertIn("structural_invalidation", incomplete["missing_fields"])

    def test_r_multiples_and_target_one_activate_protection(self):
        plan = build_exit_plan(self.base)
        result = evaluate_exit_management(self.base, plan, indicators=self.base["management_context"], config={"trailing_mode": "HYBRID"})
        self.assertAlmostEqual(result["current_r"], 1.5)
        self.assertEqual(result["peak_r"], 1.5)
        self.assertEqual(result["state"], "TARGET 1 REACHED")
        self.assertEqual(result["decision"], "TAKE PARTIAL")
        self.assertTrue(result["profit_trail_active"])
        self.assertAlmostEqual(result["mechanical_option_stop"], 2.28)

    def test_structural_stop_never_widens(self):
        plan = build_exit_plan(self.base)
        first = evaluate_exit_management(
            self.base,
            plan,
            indicators={"vwap": 101.0, "ema_slow": 101.5, "completed_candle": True},
            state={"structural_stop_price": 101.0, "peak_r": 1.0},
            config={"trailing_mode": "HYBRID"},
        )
        second = evaluate_exit_management(
            {**self.base, "underlying_price": 102.0},
            plan,
            indicators={"vwap": 99.0, "ema_slow": 99.5, "completed_candle": True},
            state={"structural_stop_price": first["stop_level"], "peak_r": first["peak_r"]},
            config={"trailing_mode": "HYBRID"},
        )
        self.assertGreaterEqual(second["stop_level"], first["stop_level"])

    def test_vwap_loss_requires_completed_candle_and_can_close(self):
        plan = build_exit_plan(self.base)
        result = evaluate_exit_management(
            {**self.base, "underlying_price": 100.0, "bid": 2.5},
            plan,
            indicators={"vwap": 101.0, "completed_candle": True},
            state={"peak_r": 1.0},
            config={"trailing_mode": "HYBRID"},
        )
        self.assertEqual(result["decision"], "CLOSE")
        self.assertEqual(result["state"], "TREND BROKEN")

    def test_exit_behavior_flags_fear_and_greed_patterns(self):
        early = detect_exit_behavior({"peak_r": 0.8, "exit_r": 0.4, "thesis_invalidated": False, "target_1_reached": False})
        self.assertTrue(early["fear_based_early_exit"])
        late = detect_exit_behavior({"peak_r": 1.8, "exit_r": -0.2, "thesis_invalidated": True})
        self.assertTrue(late["greed_based_late_exit"])


if __name__ == "__main__":
    unittest.main()
