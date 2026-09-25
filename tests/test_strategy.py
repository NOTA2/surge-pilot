import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from surgepilot.backtest import run_backtest
from surgepilot.data import Bar
from surgepilot.strategy import StrategyConfig, rise_percent

NY = ZoneInfo("America/New_York")


class StrategyTests(unittest.TestCase):
    def test_signal_uses_only_observations_at_or_before_lookback(self):
        start = datetime(2026, 9, 21, 9, 30, tzinfo=NY)
        points = [(start, Decimal("10")), (start + timedelta(minutes=4), Decimal("11"))]
        self.assertIsNone(rise_percent(points, points[-1][0], 5))
        points.append((start + timedelta(minutes=5), Decimal("12")))
        self.assertEqual(rise_percent(points, points[-1][0], 5), Decimal("20"))

    def test_entry_occurs_after_signal_and_exit_before_close(self):
        start = datetime(2026, 9, 21, 9, 30, tzinfo=NY)
        prices = [10, 10, 11, 11, 11.1, 11.1]
        bars = [Bar(start + timedelta(minutes=i), "TEST", Decimal(str(p)), Decimal(str(p)),
                    Decimal(str(p)), Decimal(str(p)), 5000) for i, p in enumerate(prices)]
        bars.append(Bar(start.replace(hour=15, minute=55), "TEST", Decimal("11.1"),
                        Decimal("11.1"), Decimal("11.1"), Decimal("11.1"), 5000))
        config = StrategyConfig(lookback_minutes=2, rise_pct=Decimal("9"))
        result = run_backtest(bars, config)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["entry_time"], bars[3].timestamp.isoformat())
        self.assertEqual(result["trades"][0]["reason"], "end_of_day")
        self.assertEqual(result["trades"][0]["exit_time"], bars[-1].timestamp.isoformat())

    def test_missing_cutoff_bar_is_visible_in_report(self):
        start = datetime(2026, 9, 21, 9, 30, tzinfo=NY)
        bars = [Bar(start + timedelta(minutes=i), "TEST", Decimal(str(p)), Decimal(str(p)),
                    Decimal(str(p)), Decimal(str(p)), 5000) for i, p in enumerate([10, 10, 11, 11, 11])]
        result = run_backtest(bars, StrategyConfig(lookback_minutes=2, rise_pct=Decimal("9")))
        self.assertEqual(result["trades"][0]["reason"], "missing_cutoff_bar")


if __name__ == "__main__":
    unittest.main()
