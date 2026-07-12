from __future__ import annotations

import unittest

from app.social_intelligence import _relevance, _spam_probability, _stance, _topics


class SocialIntelligenceTests(unittest.TestCase):
    def test_common_word_ticker_requires_finance_context(self) -> None:
        self.assertEqual(_relevance("CAT", "the cat sat on the mat", ["Caterpillar"]), 0.0)
        self.assertGreater(_relevance("CAT", "$CAT earnings beat estimates", ["Caterpillar"]), 0.9)

    def test_stance_and_topics_are_deterministic(self) -> None:
        text = "Bullish breakout after earnings beat; calls and guidance look strong"
        self.assertEqual(_stance(text), "BULLISH")
        self.assertIn("earnings", _topics(text))
        self.assertIn("technical", _topics(text))
        self.assertIn("options_flow", _topics(text))

    def test_hype_is_marked_as_risk(self) -> None:
        self.assertGreaterEqual(_spam_probability("GUARANTEED 100x moonshot!!!", "Reddit"), 0.45)


if __name__ == "__main__":
    unittest.main()
