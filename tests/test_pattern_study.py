import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from surgepilot.pattern_study import _continuation, _signals


class PatternStudyTests(unittest.TestCase):
    def test_watch_then_acceleration_uses_only_past_bars(self):
        start = datetime(2026, 9, 21, 4, 0, tzinfo=ZoneInfo("America/New_York"))
        bars = [(start, Decimal("10"), Decimal("10"), 100),
                (start + timedelta(minutes=1), Decimal("12"), Decimal("12"), 100),
                (start + timedelta(minutes=2), Decimal("13"), Decimal("13"), 100),
                (start + timedelta(minutes=3), Decimal("15"), Decimal("15"), 100)]
        first, fillable = _signals(bars, Decimal("10"), 3)
        self.assertEqual(first["prior_20pct"], 1)
        self.assertEqual(first["prior_20pct_and_3m_3pct"], 1)
        self.assertEqual(fillable["prior_20pct"], 1)
        self.assertEqual(first["15m_20pct"], 1)

    def test_sparse_bars_do_not_count_as_short_window_acceleration(self):
        start = datetime(2026, 9, 21, 4, 0, tzinfo=ZoneInfo("America/New_York"))
        bars = [(start, Decimal("10"), Decimal("10"), 100),
                (start + timedelta(minutes=10), Decimal("12"), Decimal("12"), 100)]
        first, _ = _signals(bars, Decimal("10"), len(bars))
        self.assertIn("prior_20pct", first)
        self.assertNotIn("3m_3pct", first)
        self.assertNotIn("prior_20pct_and_3m_3pct", first)

    def test_continuation_keeps_same_bar_order_ambiguous(self):
        start = datetime(2026, 9, 21, 4, 0, tzinfo=ZoneInfo("America/New_York"))
        bars = [(start, Decimal("12"), Decimal("12"), 100),
                (start + timedelta(minutes=1), Decimal("11.2"), Decimal("10"), 100)]
        opens_lows = [(start, Decimal("12"), Decimal("12")),
                      (start + timedelta(minutes=1), Decimal("10"), Decimal("9.4"))]
        self.assertIsNone(_continuation(bars, opens_lows, 0, 1))
        self.assertEqual(_continuation(bars, opens_lows, 0, 2)["outcome"], "ambiguous")
        opens_lows[1] = (opens_lows[1][0], Decimal("10"), Decimal("9.8"))
        self.assertEqual(_continuation(bars, opens_lows, 0, 2)["outcome"], "target")


if __name__ == "__main__":
    unittest.main()
