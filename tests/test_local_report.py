import tempfile
import unittest
from pathlib import Path

from surgepilot.local_report import evaluate_recall
from surgepilot.scan import connect_database, selection_summary
from surgepilot.strategy import StrategyConfig


class LocalReportTests(unittest.TestCase):
    def test_premarket_signal_catches_open_gap_without_future_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "research.sqlite")
            database = connect_database(path)
            database.execute("INSERT INTO sessions VALUES (?,?)", ("2026-09-22", 0))
            database.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)", [
                ("AAA", "2026-09-21", "NASDAQ", "10", "10", "10", "10", 1000),
                ("AAA", "2026-09-22", "NASDAQ", "15.5", "16", "15", "16", 1000),
            ])
            prices = [("04:00", "10"), ("04:01", "11"), ("04:02", "12"),
                      ("04:03", "14"), ("04:04", "15"), ("09:30", "15.5")]
            database.executemany("INSERT INTO minute VALUES (?,?,?,?,?,?,?,?)", [
                ("AAA", "2026-09-22", f"2026-09-22T{clock}:00-04:00", price,
                 price, price, price, 1000) for clock, price in prices
            ])
            database.execute("INSERT INTO premarket_log VALUES (?,?,?,?,?,?,?)",
                             ("AAA", "2026-09-22", "ok", 5, None, None, "2026-09-23"))
            database.commit()
            database.close()
            selection = selection_summary(path)
            report = evaluate_recall(path, selection, StrategyConfig(lookback_minutes=2))
            self.assertEqual(report["coverage"]["catchable_after_first_bar"], 1)
            self.assertEqual(report["selected"]["signals_before_50pct"], 1)
            self.assertEqual(report["selected"]["fills_before_50pct"], 1)
            self.assertEqual(report["coverage"]["premarket_gap_signals_before_open"], 1)


if __name__ == "__main__":
    unittest.main()
