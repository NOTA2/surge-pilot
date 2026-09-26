import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from surgepilot.discovery_study import analyze, first_signal
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

    def test_stronger_signal_waits_for_its_own_first_qualifying_close(self):
        bars = [self.bar(0, 11.2, 11.2), self.bar(1, 11.4, 11.4),
                self.bar(2, 12.3, 12.3), self.bar(3, 12.5, 12.5)]
        self.assertEqual(first_signal(bars, Decimal("10")), 2)
        self.assertEqual(first_signal(bars, Decimal("10"), short_rise=Decimal("1.10")), 3)
        self.assertIsNone(first_signal(bars[:3], Decimal("10"), short_rise=Decimal("1.10")))

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

    def test_premarket_50pct_in_daily_near_miss_is_reclassified(self):
        with TemporaryDirectory() as folder:
            db_path = str(Path(folder) / "research.sqlite")
            database = connect_database(db_path)
            database.execute("INSERT INTO sessions VALUES (?,?)", ("2026-09-21", 0))
            for symbol, daily_high, minute_high in (("WIN", "16", "16"),
                                                    ("CROSS", "14", "16"),
                                                    ("MISS", "14", "14")):
                database.execute("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)",
                                 (symbol, "2026-09-18", "NASDAQ", "10", "10", "10", "10", 100))
                database.execute("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)",
                                 (symbol, "2026-09-21", "NASDAQ", "10", daily_high,
                                  "10", "12", 100))
                for minute, price, high in ((0, "10", "10"), (1, "12.4", "12.4"),
                                            (2, minute_high, minute_high)):
                    stamp = (self.start.replace(hour=4) + timedelta(minutes=minute)).isoformat()
                    database.execute("INSERT INTO minute VALUES (?,?,?,?,?,?,?,?)",
                                     (symbol, "2026-09-21", stamp, price, high,
                                      price, price, 100))
            database.commit()
            database.close()
            result = analyze(db_path)
            self.assertEqual(result["premarket_50pct_crossovers_from_daily_near_misses"]
                             ["all"]["cases"], 1)
            self.assertEqual(result["all_hours_reclassified_observed"]["all"]["8"]
                             ["before_50pct"], 2)
            self.assertEqual(result["all_hours_reclassified_observed"]["all"]["8"]
                             ["remaining_near_miss_signals"], 1)


if __name__ == "__main__":
    unittest.main()
