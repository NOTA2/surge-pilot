import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from surgepilot.discovery_study import first_signal
from surgepilot.matched_controls import select_all_near_misses
from surgepilot.scan import connect_database


class DiscoveryStudyTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 21, 9, 30, tzinfo=ZoneInfo("America/New_York"))

    def bar(self, minute, high, close):
        return (self.start + timedelta(minutes=minute), Decimal(str(high)), Decimal(str(close)))

    def test_signal_uses_only_completed_closes(self):
        bars = [self.bar(0, 10, 10), self.bar(1, 12.5, 11),
                self.bar(2, 12.2, 12.2), self.bar(3, 17, 16)]
        self.assertEqual(first_signal(bars[:3], Decimal("10")), 2)
        self.assertEqual(first_signal(bars, Decimal("10")), 2)

    def test_regular_view_ignores_premarket_alert_but_keeps_recent_history(self):
        bars = [self.bar(-2, 10, 10), self.bar(-1, 12.2, 12.2),
                self.bar(0, 12.3, 12.3)]
        self.assertEqual(first_signal(bars, Decimal("10")), 1)
        self.assertEqual(first_signal(bars, Decimal("10"), self.start.time()), 2)

    def test_high_without_close_confirmation_is_not_signal(self):
        bars = [self.bar(0, 10, 10), self.bar(1, 15, 11.5),
                self.bar(2, 15.5, 11.8)]
        self.assertIsNone(first_signal(bars, Decimal("10")))

    def test_sparse_old_bar_cannot_create_acceleration(self):
        bars = [self.bar(0, 10, 10), self.bar(10, 12.5, 12.5)]
        self.assertIsNone(first_signal(bars, Decimal("10")))

    def test_price_must_still_be_twenty_percent_above_prior_close(self):
        bars = [self.bar(0, 10, 10), self.bar(1, 12.5, 12.5),
                self.bar(2, 10.7, 10.7), self.bar(3, 11.8, 11.8)]
        self.assertEqual(first_signal(bars, Decimal("10")), 1)
        self.assertIsNone(first_signal(bars[2:], Decimal("10")))

    def test_full_near_miss_universe_excludes_both_winner_definitions(self):
        with TemporaryDirectory() as folder:
            db_path = str(Path(folder) / "research.sqlite")
            database = connect_database(db_path)
            database.execute("INSERT INTO sessions VALUES (?,?)", ("2026-09-21", 0))
            rows = []
            for symbol, opening, high in (("MISS", "10", "14"), ("WIN", "10", "16"),
                                          ("OPENWIN", "7", "11"), ("FLAT", "10", "11")):
                rows.extend(((symbol, "2026-09-18", "NASDAQ", "10", "10", "10", "10", 100),
                             (symbol, "2026-09-21", "NASDAQ", opening, high, opening,
                              opening, 100)))
            database.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)", rows)
            database.commit()
            database.close()
            self.assertEqual([(item["symbol"], item["date"])
                              for item in select_all_near_misses(db_path)],
                             [("MISS", "2026-09-21")])


if __name__ == "__main__":
    unittest.main()
