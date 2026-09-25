import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from surgepilot.paper import PaperSession
from surgepilot.strategy import StrategyConfig

NY = ZoneInfo("America/New_York")


class PaperTests(unittest.TestCase):
    def test_three_entry_budgets_and_stale_quote_rejection(self):
        start = datetime(2026, 9, 22, 9, 30, tzinfo=NY)
        session = PaperSession(StrategyConfig(lookback_minutes=1, rise_pct=Decimal("5")),
                               Decimal("10000"), "2026-09-22")
        symbols = ("A", "B", "C", "D")
        session.update(start, [{"symbol": s, "lastPrice": "10", "timestamp": start.isoformat()} for s in symbols], True)
        later = start + timedelta(minutes=1)
        session.update(later, [{"symbol": s, "lastPrice": "11", "timestamp": later.isoformat()} for s in symbols], True)
        self.assertEqual(len(session.positions), 3)
        self.assertEqual(len(session.entered), 3)
        quantities = sorted((pos.quantity for pos in session.positions.values()))
        self.assertLess(quantities[0], quantities[2])
        self.assertGreaterEqual(session.cash, 0)
        stale_time = later + timedelta(minutes=1)
        session.update(stale_time, [{"symbol": "A", "lastPrice": "50", "timestamp": later.isoformat()}], True)
        self.assertEqual(session.prices["A"], Decimal("11"))


if __name__ == "__main__":
    unittest.main()
