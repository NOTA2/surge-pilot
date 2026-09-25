import tempfile
import unittest
from pathlib import Path

from surgepilot.scan import connect_database, selection_summary


class SelectionTests(unittest.TestCase):
    def test_prior_day_turnover_universe_has_no_same_day_lookahead(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "research.sqlite")
            db = connect_database(path)
            db.execute("INSERT INTO sessions VALUES (?,?)", ("2026-09-22", 0))
            db.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)", [
                ("AAA", "2026-09-21", "NASDAQ", "10", "10", "10", "10", 100000),
                ("BBB", "2026-09-21", "NASDAQ", "10", "10", "10", "10", 100),
                ("AAA", "2026-09-22", "NASDAQ", "10", "10", "10", "10", 100),
                ("BBB", "2026-09-22", "NASDAQ", "10", "20", "10", "20", 100000),
            ])
            db.commit()
            db.close()
            result = selection_summary(path, top_n=1)
            self.assertEqual(result["top_prior"][0]["symbols"], ["AAA"])
            self.assertEqual(result["top_same_day"][0]["symbols"], ["BBB"])


if __name__ == "__main__":
    unittest.main()
