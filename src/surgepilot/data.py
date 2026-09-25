"""CSV input and synthetic example data."""

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def load_bars(path: str | Path) -> list[Bar]:
    bars = []
    files = sorted(Path(path).glob("*.csv")) if Path(path).is_dir() else [Path(path)]
    if not files:
        raise ValueError("no CSV files found")
    for file in files:
        with file.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    raise ValueError(f"timezone required in {file}: {row['timestamp']}")
                bar = Bar(ts, row["symbol"].upper(), *(Decimal(row[k]) for k in ("open", "high", "low", "close")), int(row["volume"]))
                if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
                    raise ValueError(f"invalid OHLC in {file}: {row}")
                bars.append(bar)
    bars.sort(key=lambda b: (b.timestamp, b.symbol))
    if len({(b.timestamp, b.symbol) for b in bars}) != len(bars):
        raise ValueError("duplicate symbol/timestamp bar")
    return bars
