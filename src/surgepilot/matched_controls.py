"""Deterministic non-mover comparison set for the 30-session surge study."""

import argparse
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from .scan import RateLimiter, client_from_file, connect_database, selection_summary
from .toss import TossError

NY = ZoneInfo("America/New_York")


def select_all_near_misses(db_path: str) -> list[dict]:
    """Every daily +20% high that stays below both +50% event definitions."""
    database = connect_database(db_path)
    sessions = [row[0] for row in database.execute("SELECT date FROM sessions ORDER BY ordinal")]
    days = {}
    for symbol, date, opening, high, close in database.execute(
            "SELECT symbol,date,open,high,close FROM daily"):
        days.setdefault(date, {})[symbol] = (Decimal(opening), Decimal(high), Decimal(close))
    dates = sorted(days)
    result = []
    for date in sessions:
        previous = max((old for old in dates if old < date), default=None)
        prior_rows = days.get(previous, {})
        for symbol, (opening, high, _) in days.get(date, {}).items():
            before = prior_rows.get(symbol)
            if not before or before[2] <= 0:
                continue
            prior_close = before[2]
            if (high >= prior_close * Decimal("1.2")
                    and high < prior_close * Decimal("1.5")
                    and high < opening * Decimal("1.5")):
                result.append({"symbol": symbol, "date": date, "matched_winner": ""})
    database.close()
    return sorted(result, key=lambda item: (item["date"], item["symbol"]))


def select_controls(db_path: str, limit: int = 200,
                    kind: str = "ordinary") -> list[dict]:
    """Match sampled 50% movers on date, prior close, and prior turnover."""
    if kind == "near_miss_all":
        return select_all_near_misses(db_path)
    if limit < 1:
        raise ValueError("limit must be positive")
    if kind not in ("ordinary", "near_miss"):
        raise ValueError("kind must be ordinary, near_miss or near_miss_all")
    selection = selection_summary(db_path)
    winners = sorted(selection["winners"], key=lambda item: (item["date"], item["symbol"]))
    database = connect_database(db_path)
    days = {}
    for symbol, date, opening, high, low, close, volume in database.execute(
            "SELECT symbol,date,open,high,low,close,volume FROM daily"):
        days.setdefault(date, {})[symbol] = (Decimal(opening), Decimal(high), Decimal(low),
                                             Decimal(close), int(volume))
    sessions = selection["sessions"]
    dates = sorted(days)
    previous = {date: max((old for old in dates if old < date), default=None) for date in sessions}
    winner_pairs = {(item["symbol"], item["date"]) for item in winners}
    candidate_by_day = {}
    for date in sessions:
        prior_day = days.get(previous[date], {})
        candidates = []
        for symbol, row in days.get(date, {}).items():
            before = prior_day.get(symbol)
            if not before or (symbol, date) in winner_pairs or before[3] <= 0:
                continue
            if row[1] >= before[3] * Decimal("1.5") or row[1] >= row[0] * Decimal("1.5"):
                continue
            if kind == "near_miss" and row[1] < before[3] * Decimal("1.2"):
                continue
            turnover = (before[1] + before[2] + before[3]) / 3 * before[4]
            candidates.append((symbol, float(before[3]), float(turnover)))
        candidate_by_day[date] = candidates
    if len(winners) <= limit:
        sampled = winners
    elif limit == 1:
        sampled = [winners[len(winners) // 2]]
    else:
        sampled = [winners[round(i * (len(winners) - 1) / (limit - 1))] for i in range(limit)]
    used = set()
    result = []
    for item in sampled:
        date, source = item["date"], item["symbol"]
        before = days[previous[date]].get(source)
        if not before or before[3] <= 0:
            continue
        price = float(before[3])
        turnover = float((before[1] + before[2] + before[3]) / 3 * before[4])
        eligible = (candidate for candidate in candidate_by_day[date]
                    if (candidate[0], date) not in used)
        best = min(eligible, key=lambda candidate: (
            abs(math.log(max(candidate[1], 0.000001) / max(price, 0.000001))) +
            0.35 * abs(math.log((candidate[2] + 1) / (turnover + 1))),
            candidate[0]), default=None)
        if best is None:
            continue
        symbol, match_price, match_turnover = best
        used.add((symbol, date))
        result.append({"symbol": symbol, "date": date, "matched_winner": source,
                       "prior_close": match_price, "prior_turnover": match_turnover,
                       "winner_prior_close": price, "winner_prior_turnover": turnover})
    database.close()
    return result


def collect_controls(credentials_file: str, db_path: str, limit: int = 200,
                     workers: int = 2, rate: float = 2, kind: str = "ordinary"):
    controls = select_controls(db_path, limit, kind)
    database = connect_database(db_path)
    table = {"ordinary": "control_log", "near_miss": "near_miss_log",
             "near_miss_all": "near_miss_all_log"}[kind]
    database.execute(f"""CREATE TABLE IF NOT EXISTS {table}(
        symbol TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL, bars INTEGER NOT NULL,
        matched_winner TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(symbol,date))""")
    done = set(database.execute(f"SELECT symbol,date FROM {table} WHERE status='ok'"))
    if kind == "near_miss_all" and database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='near_miss_log'").fetchone():
        done.update(database.execute("SELECT symbol,date FROM near_miss_log WHERE status='ok'"))
    todo = [item for item in controls if (item["symbol"], item["date"]) not in done]
    print(f"{kind} controls={len(controls)} remaining={len(todo)}", flush=True)
    if not todo:
        database.close()
        return
    client = client_from_file(credentials_file)
    client._authenticate()
    limiter = RateLimiter(rate)

    def fetch(item):
        symbol, date = item["symbol"], item["date"]
        start = datetime.combine(datetime.fromisoformat(date).date(), time(4), NY)
        end = datetime.combine(datetime.fromisoformat(date).date(), time(15, 59), NY)
        before = end.isoformat()
        rows, seen = [], set()
        try:
            for _ in range(8):
                limiter.wait()
                page = client.candles(symbol, before)
                candles = page.get("candles", [])
                if not candles:
                    break
                for candle in candles:
                    stamp = datetime.fromisoformat(candle["timestamp"]).astimezone(NY)
                    if start <= stamp <= end and candle["timestamp"] not in seen:
                        seen.add(candle["timestamp"])
                        rows.append((symbol, date, candle["timestamp"], candle["openPrice"],
                                     candle["highPrice"], candle["lowPrice"],
                                     candle["closePrice"], int(candle["volume"])))
                oldest = datetime.fromisoformat(candles[-1]["timestamp"]).astimezone(NY)
                next_before = page.get("nextBefore")
                if oldest < start or not next_before or next_before == before:
                    break
                before = next_before
            return item, sorted(rows, key=lambda row: row[2]), "ok" if rows else "empty"
        except (TossError, KeyError, ValueError) as error:
            match = re.search(r"\b(\d{3})\b", str(error))
            return item, rows, f"error:{match.group(1) if match else type(error).__name__}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(fetch, item) for item in todo]
        for count, future in enumerate(as_completed(pending), 1):
            item, rows, status = future.result()
            if rows:
                database.executemany("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?,?,?,?)", rows)
            database.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?)",
                             (item["symbol"], item["date"], status, len(rows), item["matched_winner"],
                              datetime.now(NY).isoformat()))
            if count % 25 == 0 or count == len(todo):
                database.commit()
                print(f"{kind} controls scanned {count}/{len(todo)}", flush=True)
    database.close()


def main():
    parser = argparse.ArgumentParser(description="Matched non-mover control study")
    parser.add_argument("--db", default="data/private/research.sqlite")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--kind", choices=["ordinary", "near_miss", "near_miss_all"], default="ordinary")
    parser.add_argument("--credentials-file")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--rate", type=float, default=2)
    args = parser.parse_args()
    if args.credentials_file:
        collect_controls(args.credentials_file, args.db, args.limit, args.workers, args.rate, args.kind)
    else:
        controls = select_controls(args.db, args.limit, args.kind)
        print(f"{args.kind} controls selected={len(controls)}")
        for key in ("prior_close", "prior_turnover") if args.kind != "near_miss_all" else ():
            import statistics
            ratios = [item[key] / max(item["winner_" + key], 0.000001) for item in controls]
            print(f"median {key} ratio={statistics.median(ratios):.2f}")


if __name__ == "__main__":
    main()
