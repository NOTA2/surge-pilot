"""Clearly synthetic minute bars for smoke tests and the public dashboard."""

import csv
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def write_demo(path: str | Path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    dates = [(2026, 9, day) for day in (14, 15, 16, 17, 18, 21, 22, 23)]
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        for day_index, (year, month, day) in enumerate(dates):
            start = datetime(year, month, day, 9, 30, tzinfo=NY)
            previous = {"SPKE": Decimal("10"), "RUSH": Decimal("18"), "CALM": Decimal("32"), "DULL": Decimal("45")}
            for minute in range(390):
                ts = start + timedelta(minutes=minute)
                for symbol in previous:
                    old = previous[symbol]
                    if symbol == "SPKE" and day_index in (1, 3, 6) and 22 <= minute < 44:
                        change = Decimal("0.025") if day_index == 6 else Decimal("0.013")
                    elif symbol == "SPKE" and day_index in (1, 3, 6) and 44 <= minute < 60:
                        change = Decimal("-0.008")
                    elif symbol == "RUSH" and day_index in (2, 5) and 65 <= minute < 82:
                        change = Decimal("0.008")
                    elif symbol == "RUSH" and day_index in (2, 5) and 82 <= minute < 95:
                        change = Decimal("-0.009")
                    else:
                        change = Decimal(((minute * 7 + day_index * 11 + len(symbol)) % 9) - 4) / Decimal("10000")
                    close = (old * (1 + change)).quantize(Decimal("0.0001"))
                    high = max(old, close) * Decimal("1.001")
                    low = min(old, close) * Decimal("0.999")
                    volume = 5000 if symbol in ("SPKE", "RUSH") else 1200
                    writer.writerow([ts.isoformat(), symbol, old, high.quantize(Decimal("0.0001")),
                                     low.quantize(Decimal("0.0001")), close, volume])
                    previous[symbol] = close
