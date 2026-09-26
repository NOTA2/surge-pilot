"""Resumable US daily universe scan for historical research.

Credentials are read into memory and never stored in the SQLite database.
"""

import argparse
import json
import re
import sqlite3
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, time as wall_time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import run_backtest
from .data import Bar
from .strategy import StrategyConfig
from .toss import TossClient, TossError

NY = ZoneInfo("America/New_York")


def client_from_file(path: str) -> TossClient:
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if (len(lines) != 4 or lines[0].lower().replace(" ", "_") != "client_id"
            or lines[2].lower().replace(" ", "_") != "client_secret"):
        raise ValueError("credentials file must contain client id and client secret labels and values")
    return TossClient(client_id=lines[1], client_secret=lines[3])


class RateLimiter:
    def __init__(self, calls_per_second: float):
        self.interval = 1 / calls_per_second
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            scheduled = max(now, self.next_at)
            self.next_at = scheduled + self.interval
        delay = scheduled - now
        if delay > 0:
            time.sleep(delay)


def connect_database(path: str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    connection.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS universe(symbol TEXT PRIMARY KEY, market TEXT NOT NULL, name TEXT);
      CREATE TABLE IF NOT EXISTS sessions(date TEXT PRIMARY KEY, ordinal INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS daily(symbol TEXT NOT NULL, date TEXT NOT NULL, market TEXT NOT NULL,
        open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
        volume INTEGER NOT NULL, PRIMARY KEY(symbol,date));
      CREATE TABLE IF NOT EXISTS scan_log(symbol TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS minute(symbol TEXT NOT NULL, date TEXT NOT NULL, timestamp TEXT NOT NULL,
        open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
        volume INTEGER NOT NULL, PRIMARY KEY(symbol,timestamp));
      CREATE TABLE IF NOT EXISTS minute_log(symbol TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
        bars INTEGER NOT NULL, first_at TEXT, last_at TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(symbol,date));
      CREATE TABLE IF NOT EXISTS premarket_log(symbol TEXT NOT NULL, date TEXT NOT NULL, status TEXT NOT NULL,
        bars INTEGER NOT NULL, first_at TEXT, last_at TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(symbol,date));
    """)
    return connection


def _date(candle: dict) -> str:
    return datetime.fromisoformat(candle["timestamp"]).astimezone(NY).date().isoformat()


def scan_daily(client: TossClient, db_path: str, days: int = 30, workers: int = 8, calls_per_second: float = 8):
    connection = connect_database(db_path)
    anchor = client._request("GET", "/api/v1/candles", {"symbol": "AAPL", "interval": "1d",
                           "count": min(200, days + 15), "adjusted": "false"})["candles"]
    now = datetime.now(NY)
    complete_days = sorted({_date(candle) for candle in anchor
                            if _date(candle) < now.date().isoformat()
                            or (_date(candle) == now.date().isoformat() and now.time() >= wall_time(16, 5))})
    if len(complete_days) < days + 1:
        raise ValueError("not enough completed sessions in AAPL daily history")
    selected = complete_days[-days:]
    first_index = complete_days.index(selected[0])
    preceding = complete_days[first_index - 1]
    connection.executemany("INSERT OR REPLACE INTO sessions(date,ordinal) VALUES (?,?)",
                           [(day, i) for i, day in enumerate(selected)])
    connection.commit()

    all_stocks: dict[str, tuple[str, str]] = {}
    for market in ("NASDAQ", "NYSE", "AMEX"):
        for item in client.stocks(market):
            all_stocks[item["symbol"]] = (market, item.get("name", ""))
        time.sleep(1.05)  # STOCK_ALL: 1 request per second.
    connection.executemany("INSERT OR REPLACE INTO universe(symbol,market,name) VALUES (?,?,?)",
                           [(symbol, market, name) for symbol, (market, name) in all_stocks.items()])
    connection.commit()
    done = {row[0] for row in connection.execute("SELECT symbol FROM scan_log WHERE status='ok'")}
    todo = [symbol for symbol in sorted(all_stocks) if symbol not in done]
    print(f"sessions={selected[0]}..{selected[-1]} stocks={len(all_stocks)} remaining={len(todo)}", flush=True)
    limiter = RateLimiter(calls_per_second)

    def fetch(symbol: str):
        limiter.wait()
        try:
            page = client._request("GET", "/api/v1/candles", {"symbol": symbol, "interval": "1d",
                                   "count": min(200, days + 15), "adjusted": "false"})
            rows = [(symbol, _date(c), all_stocks[symbol][0], c["openPrice"], c["highPrice"],
                     c["lowPrice"], c["closePrice"], int(c["volume"])) for c in page["candles"]
                    if preceding <= _date(c) <= selected[-1]]
            return symbol, rows, "ok"
        except (TossError, KeyError, ValueError) as error:
            match = re.search(r"\b(\d{3})\b", str(error))
            return symbol, [], f"error:{match.group(1) if match else type(error).__name__}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(fetch, symbol) for symbol in todo]
        for count, future in enumerate(as_completed(pending), 1):
            symbol, rows, status = future.result()
            if rows:
                connection.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?)", rows)
            connection.execute("INSERT OR REPLACE INTO scan_log VALUES (?,?,?)",
                               (symbol, status, datetime.now(NY).isoformat()))
            if count % 100 == 0 or count == len(todo):
                connection.commit()
                print(f"daily scanned {count}/{len(todo)}", flush=True)
    connection.close()
    return {"sessions": selected, "stocks": len(all_stocks), "database": db_path}


def selection_summary(db_path: str, top_n: int = 100) -> dict:
    connection = connect_database(db_path)
    sessions = [row[0] for row in connection.execute("SELECT date FROM sessions ORDER BY ordinal")]
    if not sessions:
        raise ValueError("no session dates in database")
    all_dates = sorted({row[0] for row in connection.execute("SELECT DISTINCT date FROM daily")})
    previous = {}
    for date in sessions:
        earlier = [d for d in all_dates if d < date]
        previous[date] = earlier[-1] if earlier else None
    bars_by_day = {}
    for row in connection.execute("SELECT symbol,date,open,high,low,close,volume FROM daily"):
        bars_by_day.setdefault(row[1], {})[row[0]] = row
    winners = []
    rankings = {}
    for date, by_symbol in bars_by_day.items():
        values = []
        for symbol, _, opening, high, low, close, volume in by_symbol.values():
            estimate = sum(map(Decimal, (high, low, close))) / 3 * volume
            values.append((estimate, symbol))
        values.sort(reverse=True)
        rankings[date] = [symbol for _, symbol in values]
    top_prior = []
    top_same_day = []
    for date in sessions:
        day_rows = bars_by_day.get(date, {})
        prior_rows = bars_by_day.get(previous[date], {})
        for symbol, _, opening, high, low, close, volume in day_rows.values():
            opening, high, low, close = map(Decimal, (opening, high, low, close))
            prior = prior_rows.get(symbol)
            if prior and Decimal(prior[5]) > 0:
                prior_close = Decimal(prior[5])
                if high / prior_close >= Decimal("1.5") or high / opening >= Decimal("1.5"):
                    winners.append({"date": date, "symbol": symbol,
                                    "prev_close_to_high_pct": round(float((high / prior_close - 1) * 100), 2),
                                    "open_to_high_pct": round(float((high / opening - 1) * 100), 2)})
        top_prior.append({"date": date, "ranked_on": previous[date],
                          "symbols": rankings.get(previous[date], [])[:top_n],
                          "covered": len(prior_rows)})
        top_same_day.append({"date": date, "symbols": rankings.get(date, [])[:top_n],
                             "covered": len(day_rows)})
    connection.close()
    return {"sessions": sessions, "winners": winners, "top_prior": top_prior,
            "top_same_day": top_same_day,
            "turnover_method": "daily volume × (high + low + close) / 3; approximate",
            "selection_rule": "prior-day top is known before entry; same-day top is diagnostic only"}


def scan_minutes(client: TossClient, db_path: str, phase: str = "winners", top_n: int = 100,
                 workers: int = 8, calls_per_second: float = 8):
    selection = selection_summary(db_path, top_n)
    if phase == "winners":
        pairs = {(item["symbol"], item["date"]) for item in selection["winners"]}
    elif phase == "top_prior":
        pairs = {(symbol, item["date"]) for item in selection["top_prior"] for symbol in item["symbols"]}
    else:
        raise ValueError("phase must be winners or top_prior")
    connection = connect_database(db_path)
    done = set(connection.execute("SELECT symbol,date FROM minute_log WHERE status='ok'"))
    todo = sorted(pairs - done)
    print(f"phase={phase} pairs={len(pairs)} remaining={len(todo)}", flush=True)
    limiter = RateLimiter(calls_per_second)

    def fetch(pair: tuple[str, str]):
        symbol, date = pair
        end = datetime.combine(datetime.fromisoformat(date).date(), wall_time(15, 59), NY)
        start = datetime.combine(datetime.fromisoformat(date).date(), wall_time(9, 30), NY)
        before = end.isoformat()
        seen = set()
        rows = []
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
                                     candle["highPrice"], candle["lowPrice"], candle["closePrice"], int(candle["volume"])))
                oldest = datetime.fromisoformat(candles[-1]["timestamp"]).astimezone(NY)
                next_before = page.get("nextBefore")
                if oldest < start or not next_before or next_before == before:
                    break
                before = next_before
            rows.sort(key=lambda row: row[2])
            status = "ok" if rows else "empty"
            return pair, rows, status
        except (TossError, KeyError, ValueError) as error:
            match = re.search(r"\b(\d{3})\b", str(error))
            return pair, rows, f"error:{match.group(1) if match else type(error).__name__}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(fetch, pair) for pair in todo]
        for count, future in enumerate(as_completed(pending), 1):
            (symbol, date), rows, status = future.result()
            if rows:
                connection.executemany("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?,?,?,?)", rows)
            connection.execute("INSERT OR REPLACE INTO minute_log VALUES (?,?,?,?,?,?,?)",
                               (symbol, date, status, len(rows), rows[0][2] if rows else None,
                                rows[-1][2] if rows else None, datetime.now(NY).isoformat()))
            if count % 50 == 0 or count == len(todo):
                connection.commit()
                print(f"minute pairs scanned {count}/{len(todo)}", flush=True)
    connection.close()
    return {"pairs": len(pairs), "database": db_path}


def scan_premarket(client: TossClient, db_path: str, workers: int = 8,
                   calls_per_second: float = 8):
    """Add 04:00-09:29 ET candles for the known 50% regular-session cases."""
    # Fail before starting the worker pool if token issuance is temporarily
    # unavailable. Otherwise every queued worker can retry authentication.
    try:
        client._authenticate()
    except TossError as error:
        raise SystemExit(f"premarket collection stopped before requests: {error}; check Toss API allowlisted IP") from None
    selection = selection_summary(db_path)
    pairs = {(item["symbol"], item["date"]) for item in selection["winners"]}
    connection = connect_database(db_path)
    done = set(connection.execute("SELECT symbol,date FROM premarket_log WHERE status='ok'"))
    todo = sorted(pairs - done)
    print(f"premarket pairs={len(pairs)} remaining={len(todo)}", flush=True)
    limiter = RateLimiter(calls_per_second)

    def fetch(pair: tuple[str, str]):
        symbol, date = pair
        start = datetime.combine(datetime.fromisoformat(date).date(), wall_time(4, 0), NY)
        end = datetime.combine(datetime.fromisoformat(date).date(), wall_time(9, 29), NY)
        before = end.isoformat()
        rows = []
        seen = set()
        try:
            for _ in range(6):
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
                                     candle["highPrice"], candle["lowPrice"], candle["closePrice"], int(candle["volume"])))
                oldest = datetime.fromisoformat(candles[-1]["timestamp"]).astimezone(NY)
                next_before = page.get("nextBefore")
                if oldest < start or not next_before or next_before == before:
                    break
                before = next_before
            rows.sort(key=lambda row: row[2])
            return pair, rows, "ok" if rows else "empty"
        except (TossError, KeyError, ValueError) as error:
            match = re.search(r"\b(\d{3})\b", str(error))
            return pair, rows, f"error:{match.group(1) if match else type(error).__name__}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(fetch, pair) for pair in todo]
        for count, future in enumerate(as_completed(pending), 1):
            (symbol, date), rows, status = future.result()
            if rows:
                connection.executemany("INSERT OR REPLACE INTO minute VALUES (?,?,?,?,?,?,?,?)", rows)
            connection.execute("INSERT OR REPLACE INTO premarket_log VALUES (?,?,?,?,?,?,?)",
                               (symbol, date, status, len(rows), rows[0][2] if rows else None,
                                rows[-1][2] if rows else None, datetime.now(NY).isoformat()))
            if count % 50 == 0 or count == len(todo):
                connection.commit()
                print(f"premarket scanned {count}/{len(todo)}", flush=True)
    connection.close()
    return {"pairs": len(pairs), "database": db_path}


def export_minutes(db_path: str, path: str, phase: str, top_n: int = 100):
    import csv
    selection = selection_summary(db_path, top_n)
    if phase == "winners":
        pairs = {(item["symbol"], item["date"]) for item in selection["winners"]}
    elif phase == "top_prior":
        pairs = {(symbol, item["date"]) for item in selection["top_prior"] for symbol in item["symbols"]}
    else:
        raise ValueError("phase must be winners or top_prior")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(db_path)
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        for symbol, date in sorted(pairs):
            for row in connection.execute("SELECT timestamp,open,high,low,close,volume FROM minute WHERE symbol=? AND date=? ORDER BY timestamp", (symbol, date)):
                writer.writerow([row[0], symbol, *row[1:]])
    connection.close()
    print(f"exported {len(pairs)} symbol-days to {target}")


def _phase_pairs(selection: dict, phase: str) -> set[tuple[str, str]]:
    if phase == "winners":
        return {(item["symbol"], item["date"]) for item in selection["winners"]}
    if phase == "top_prior":
        return {(symbol, item["date"]) for item in selection["top_prior"] for symbol in item["symbols"]}
    raise ValueError("phase must be winners or top_prior")


def iter_selected_bars(db_path: str, pairs: set[tuple[str, str]]):
    connection = connect_database(db_path)
    try:
        connection.execute("CREATE TEMP TABLE chosen(symbol TEXT NOT NULL,date TEXT NOT NULL,PRIMARY KEY(symbol,date))")
        connection.executemany("INSERT INTO chosen VALUES (?,?)", pairs)
        query = """SELECT m.timestamp,m.symbol,m.open,m.high,m.low,m.close,m.volume
                   FROM minute m JOIN chosen c ON m.symbol=c.symbol AND m.date=c.date
                   ORDER BY m.timestamp,m.symbol"""
        for stamp, symbol, opening, high, low, close, volume in connection.execute(query):
            yield Bar(datetime.fromisoformat(stamp), symbol, Decimal(opening), Decimal(high),
                      Decimal(low), Decimal(close), volume)
    finally:
        connection.close()


def run_phase_report(db_path: str, phase: str, output: str, top_n: int = 100,
                     config: StrategyConfig | None = None) -> dict:
    selection = selection_summary(db_path, top_n)
    pairs = _phase_pairs(selection, phase)
    connection = connect_database(db_path)
    logs = {tuple(row[:2]): row[2:] for row in connection.execute("SELECT symbol,date,status,bars,first_at,last_at FROM minute_log")}
    covered = sum(pair in logs and logs[pair][0] == "ok" for pair in pairs)
    full_day = sum(pair in logs and logs[pair][0] == "ok" and
                   logs[pair][2] and logs[pair][3] and
                   datetime.fromisoformat(logs[pair][2]).astimezone(NY).time() <= wall_time(9, 35) and
                   datetime.fromisoformat(logs[pair][3]).astimezone(NY).time() >= wall_time(15, 55)
                   for pair in pairs)
    connection.close()
    if covered == 0:
        raise ValueError("no minute bars collected for this phase")
    report = run_backtest(iter_selected_bars(db_path, pairs), config or StrategyConfig(), assume_sorted=True)
    report["kind"] = "historical_phase"
    report["source"] = "toss_historical"
    report["phase"] = phase
    report["period"] = [selection["sessions"][0], selection["sessions"][-1]]
    report["coverage"] = {"selected_symbol_days": len(pairs), "with_minute_data": covered,
                          "near_full_regular_session": full_day,
                          "coverage_pct": round(covered / len(pairs) * 100, 2) if pairs else 0}
    if phase == "winners":
        report["event_definition"] = "daily high at least 50% above prior close OR daily open, selected after the fact"
        report["candidate_events"] = selection["winners"]
        report["event_categories"] = {
            "open_to_high_50pct": sum(item["open_to_high_pct"] >= 50 for item in selection["winners"]),
            "prior_close_only_50pct": sum(item["open_to_high_pct"] < 50 for item in selection["winners"]),
        }
        connection = connect_database(db_path)
        signalled = {(signal["symbol"], datetime.fromisoformat(signal["time"]).astimezone(NY).date().isoformat()): []
                     for signal in report["signals"]}
        bought = {}
        for signal in report["signals"]:
            pair = (signal["symbol"], datetime.fromisoformat(signal["time"]).astimezone(NY).date().isoformat())
            signalled[pair].append(datetime.fromisoformat(signal["time"]))
            if signal["decision"] == "bought":
                bought.setdefault(pair, []).append(datetime.fromisoformat(signal["fill_time"]))
        reached_regular = 0
        caught_early = 0
        bought_early = 0
        already_at_open = 0
        first_minute_hit = 0
        for event in selection["winners"]:
            symbol, date = event["symbol"], event["date"]
            day = connection.execute("SELECT open FROM daily WHERE symbol=? AND date=?", (symbol, date)).fetchone()
            prior = connection.execute("SELECT close FROM daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1", (symbol, date)).fetchone()
            if not day or not prior:
                continue
            thresholds = (Decimal(day[0]) * Decimal("1.5"), Decimal(prior[0]) * Decimal("1.5"))
            rows = connection.execute("SELECT timestamp,open,high FROM minute WHERE symbol=? AND date=? ORDER BY timestamp", (symbol, date)).fetchall()
            hit = next((datetime.fromisoformat(stamp) for stamp, _, high in rows
                        if any(Decimal(high) >= threshold for threshold in thresholds)), None)
            if hit is None:
                continue
            reached_regular += 1
            if rows and any(Decimal(rows[0][1]) >= threshold for threshold in thresholds):
                already_at_open += 1
            if rows and datetime.fromisoformat(rows[0][0]) == hit:
                first_minute_hit += 1
            if any(stamp < hit for stamp in signalled.get((symbol, date), [])):
                caught_early += 1
            if any(stamp < hit for stamp in bought.get((symbol, date), [])):
                bought_early += 1
        connection.close()
        catchable = reached_regular - first_minute_hit
        report["event_analysis"] = {"daily_50pct_events": len(selection["winners"]),
                                    "reached_50pct_in_regular_session": reached_regular,
                                    "already_50pct_at_regular_open": already_at_open,
                                    "reached_50pct_in_first_minute": first_minute_hit,
                                    "catchable_after_first_minute": catchable,
                                    "signalled_before_50pct": caught_early,
                                    "bought_before_50pct": bought_early,
                                    "early_signal_recall_pct": round(caught_early / catchable * 100, 2)
                                    if catchable else None}
    else:
        report["selection"] = f"previous trading day's approximate dollar turnover top {top_n}"
        report["turnover_method"] = selection["turnover_method"]
    report["limitations"].extend([
        "전체 종목 목록은 현재 거래 가능 종목이므로 과거 상장폐지 종목이 빠질 수 있습니다.",
        "일봉 가격·거래량 기반 거래대금은 추정치이며 실제 체결 거래대금이 아닙니다.",
        "분봉 누락이나 장 마감 근처 데이터 부족은 coverage 수치를 확인해야 합니다.",
        "50% 급등 사례만 모은 단계는 사후 선정 편향이 있어 그 손익률을 실제 운용 수익률로 해석할 수 없습니다.",
        "전일 거래대금 상위 100종목은 당일 새로 급등한 저유동성 종목을 포함하지 않을 수 있습니다.",
        "수정 전 가격은 액면병합·분할 영향을 받을 수 있으므로 급등 사례의 기업행위를 추가 확인해야 합니다.",
    ])
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"phase={phase} pairs={len(pairs)} covered={covered} trades={report['summary']['trades']} report={target}")
    return report


def run_liquidity_study(db_path: str, phase: str, output: str, top_n: int = 100,
                        thresholds: tuple[int, ...] = (0, 100000, 500000, 1000000, 5000000)) -> dict:
    """Compare signal-time cumulative dollar-volume gates on the same universe."""
    selection = selection_summary(db_path, top_n)
    pairs = _phase_pairs(selection, phase)
    hits = {}
    if phase == "winners":
        connection = connect_database(db_path)
        for event in selection["winners"]:
            symbol, date = event["symbol"], event["date"]
            day = connection.execute("SELECT open FROM daily WHERE symbol=? AND date=?", (symbol, date)).fetchone()
            prior = connection.execute("SELECT close FROM daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1", (symbol, date)).fetchone()
            if not day or not prior:
                continue
            threshold = min(Decimal(day[0]), Decimal(prior[0])) * Decimal("1.5")
            for stamp, opening, high in connection.execute("SELECT timestamp,open,high FROM minute WHERE symbol=? AND date=? ORDER BY timestamp", (symbol, date)):
                if Decimal(high) >= threshold:
                    hits[(symbol, date)] = datetime.fromisoformat(stamp)
                    break
        connection.close()
    results = []
    for threshold in thresholds:
        config = replace(StrategyConfig(), min_cumulative_dollar_volume=Decimal(threshold))
        report = run_backtest(iter_selected_bars(db_path, pairs), config, assume_sorted=True)
        bought = [signal for signal in report["signals"] if signal["decision"] == "bought"]
        early_signalled = set()
        early_bought = set()
        for signal in report["signals"]:
            stamp = datetime.fromisoformat(signal["time"])
            pair = (signal["symbol"], stamp.astimezone(NY).date().isoformat())
            if pair in hits and stamp < hits[pair]:
                early_signalled.add(pair)
                if signal["decision"] == "bought" and datetime.fromisoformat(signal["fill_time"]) < hits[pair]:
                    early_bought.add(pair)
        results.append({"min_cumulative_dollar_volume_usd": threshold,
                        "trades": report["summary"]["trades"],
                        "net_return_pct": report["summary"]["net_return_pct"],
                        "max_drawdown_pct": report["summary"]["max_drawdown_pct"],
                        "win_rate_pct": report["summary"]["win_rate_pct"],
                        "bought_symbol_days": len({(signal["symbol"],
                                                     datetime.fromisoformat(signal["time"]).astimezone(NY).date().isoformat())
                                                    for signal in bought}),
                        "early_signal_events": len(early_signalled) if phase == "winners" else None,
                        "early_bought_events": len(early_bought) if phase == "winners" else None,
                        "first_buys": [{"symbol": signal["symbol"], "date":
                                        datetime.fromisoformat(signal["time"]).astimezone(NY).date().isoformat(),
                                        "time": signal["time"]} for signal in bought]})
        print(f"liquidity threshold={threshold} trades={report['summary']['trades']}", flush=True)
    baseline = {(entry["symbol"], entry["date"]): datetime.fromisoformat(entry["time"])
                for entry in results[0]["first_buys"]}
    for result in results:
        current = {(entry["symbol"], entry["date"]): datetime.fromisoformat(entry["time"])
                   for entry in result["first_buys"]}
        shared = baseline.keys() & current.keys()
        delays = [(current[pair] - baseline[pair]).total_seconds() / 60 for pair in shared]
        result["baseline_buys_missed"] = len(baseline.keys() - current.keys())
        result["median_shared_entry_delay_minutes"] = round(statistics.median(delays), 2) if delays else None
    result = {"kind": "liquidity_study", "source": "toss_historical",
              "phase": phase, "period": [selection["sessions"][0], selection["sessions"][-1]],
              "selected_symbol_days": len(pairs), "results": results,
              "note": "Dollar volume accumulates from observed regular-session one-minute close × volume; gate is checked at signal time, not after close."}
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def run_cost_study(db_path: str, output: str, top_n: int = 100,
                   slippage_bps: tuple[int, ...] = (15, 50, 100)) -> dict:
    selection = selection_summary(db_path, top_n)
    pairs = _phase_pairs(selection, "top_prior")
    results = []
    for bps in slippage_bps:
        config = replace(StrategyConfig(), slippage_bps=Decimal(bps))
        report = run_backtest(iter_selected_bars(db_path, pairs), config, assume_sorted=True)
        results.append({"slippage_bps_each_side": bps, "fee_bps_each_side": int(config.fee_bps),
                        "trades": report["summary"]["trades"],
                        "net_return_pct": report["summary"]["net_return_pct"],
                        "max_drawdown_pct": report["summary"]["max_drawdown_pct"]})
        print(f"cost slippage_bps={bps} return={report['summary']['net_return_pct']}", flush=True)
    result = {"kind": "cost_study", "source": "toss_historical",
              "phase": "top_prior", "period": [selection["sessions"][0], selection["sessions"][-1]],
              "results": results}
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def publish_aggregate_summary(db_path: str, output: str,
                              dashboard_output: str = "docs/report.json",
                              winner_report: str = "reports/private/winners.json",
                              top_report: str = "reports/private/top100.json",
                              winner_study: str = "reports/private/winners-liquidity.json",
                              top_study: str = "reports/private/top100-liquidity.json",
                              cost_study: str = "reports/private/top100-costs.json") -> dict:
    """Write only public aggregate counts and returns, without bars or transactions."""
    def read_optional(path: str):
        target = Path(path)
        return json.loads(target.read_text(encoding="utf-8")) if target.exists() else None

    selection = selection_summary(db_path, 100)
    connection = connect_database(db_path)
    universe_stocks = connection.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    connection.close()
    winner = read_optional(winner_report)
    top = read_optional(top_report)
    wstudy = read_optional(winner_study)
    tstudy = read_optional(top_study)
    cstudy = read_optional(cost_study)
    wresults = {item["min_cumulative_dollar_volume_usd"]: item for item in wstudy["results"]} if wstudy else {}
    tresults = {item["min_cumulative_dollar_volume_usd"]: item for item in tstudy["results"]} if tstudy else {}
    thresholds = sorted(set(wresults) | set(tresults))
    summary = {"generated_at": datetime.now(NY).isoformat(),
               "period": [selection["sessions"][0], selection["sessions"][-1]],
               "session_count": len(selection["sessions"]), "universe_stocks": universe_stocks,
               "winners": {"events": len(selection["winners"]),
                           "open_to_high_50pct": sum(item["open_to_high_pct"] >= 50 for item in selection["winners"]),
                           "prior_close_only_50pct": sum(item["open_to_high_pct"] < 50 for item in selection["winners"]),
                           "minute_covered": winner["coverage"]["with_minute_data"] if winner else None,
                           "near_full": winner["coverage"]["near_full_regular_session"] if winner else None,
                           "reached_50pct": winner["event_analysis"]["reached_50pct_in_regular_session"] if winner else None,
                           "catchable_after_first_minute": winner["event_analysis"]["catchable_after_first_minute"] if winner else None,
                           "early_signals": winner["event_analysis"]["signalled_before_50pct"] if winner else None},
               "top100": {"pairs": sum(len(item["symbols"]) for item in selection["top_prior"]),
                          "minute_covered": top["coverage"]["with_minute_data"] if top else None,
                          "near_full": top["coverage"]["near_full_regular_session"] if top else None,
                          "trades": top["summary"]["trades"] if top else None,
                          "signals": top["summary"]["signals"] if top else None,
                          "days_with_trade": len({datetime.fromisoformat(item["entry_time"]).astimezone(NY).date().isoformat()
                                                  for item in top["trades"]}) if top else None,
                          "net_return_pct": top["summary"]["net_return_pct"] if top else None,
                          "max_drawdown_pct": top["summary"]["max_drawdown_pct"] if top else None,
                          "win_rate_pct": top["summary"]["win_rate_pct"] if top else None},
               "liquidity": [{"threshold_usd": threshold,
                              "winners_trades": wresults.get(threshold, {}).get("trades"),
                              "winners_early_signal_events": wresults.get(threshold, {}).get("early_signal_events"),
                              "winners_early_bought_events": wresults.get(threshold, {}).get("early_bought_events"),
                              "winners_baseline_buys_missed": wresults.get(threshold, {}).get("baseline_buys_missed"),
                              "winners_median_entry_delay_minutes": wresults.get(threshold, {}).get("median_shared_entry_delay_minutes"),
                              "top100_trades": tresults.get(threshold, {}).get("trades"),
                              "top100_return_pct": tresults.get(threshold, {}).get("net_return_pct")}
                             for threshold in thresholds],
               "costs": cstudy["results"] if cstudy else []}
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if top:
        daily_equity = {}
        for point in top["equity"]:
            key = datetime.fromisoformat(point["time"]).astimezone(NY).date().isoformat()
            daily_equity[key] = point
        public_report = {key: top[key] for key in ("kind", "source", "phase", "period", "strategy",
                                                   "summary", "coverage", "selection", "turnover_method", "limitations")}
        public_report.update({"aggregate_only": True, "equity": [daily_equity[key] for key in sorted(daily_equity)],
                              "trades": [], "signals": []})
        dashboard_target = Path(dashboard_output)
        dashboard_target.parent.mkdir(parents=True, exist_ok=True)
        dashboard_target.write_text(json.dumps(public_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(prog="python -m surgepilot.scan")
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily")
    daily.add_argument("--credentials-file", required=True)
    daily.add_argument("--db", default="data/private/research.sqlite")
    daily.add_argument("--days", type=int, default=30)
    daily.add_argument("--workers", type=int, default=8)
    daily.add_argument("--rate", type=float, default=8)
    summarize = sub.add_parser("summarize")
    summarize.add_argument("--db", default="data/private/research.sqlite")
    summarize.add_argument("--top", type=int, default=100)
    minute = sub.add_parser("minute")
    minute.add_argument("--credentials-file", required=True)
    minute.add_argument("--db", default="data/private/research.sqlite")
    minute.add_argument("--phase", choices=["winners", "top_prior"], default="winners")
    minute.add_argument("--top", type=int, default=100)
    minute.add_argument("--workers", type=int, default=8)
    minute.add_argument("--rate", type=float, default=8)
    premarket = sub.add_parser("premarket")
    premarket.add_argument("--credentials-file", required=True)
    premarket.add_argument("--db", default="data/private/research.sqlite")
    premarket.add_argument("--workers", type=int, default=8)
    premarket.add_argument("--rate", type=float, default=8)
    export = sub.add_parser("export")
    export.add_argument("--db", default="data/private/research.sqlite")
    export.add_argument("--phase", choices=["winners", "top_prior"], required=True)
    export.add_argument("--top", type=int, default=100)
    export.add_argument("--output", required=True)
    report = sub.add_parser("report")
    report.add_argument("--db", default="data/private/research.sqlite")
    report.add_argument("--phase", choices=["winners", "top_prior"], required=True)
    report.add_argument("--top", type=int, default=100)
    report.add_argument("--output", default="reports/private/phase.json")
    study = sub.add_parser("study")
    study.add_argument("--db", default="data/private/research.sqlite")
    study.add_argument("--phase", choices=["winners", "top_prior"], required=True)
    study.add_argument("--top", type=int, default=100)
    study.add_argument("--output", default="reports/private/liquidity.json")
    cost = sub.add_parser("cost-study")
    cost.add_argument("--db", default="data/private/research.sqlite")
    cost.add_argument("--top", type=int, default=100)
    cost.add_argument("--output", default="reports/private/top100-costs.json")
    publish = sub.add_parser("publish")
    publish.add_argument("--db", default="data/private/research.sqlite")
    publish.add_argument("--output", default="docs/study-summary.json")
    args = parser.parse_args()
    if args.command == "daily":
        scan_daily(client_from_file(args.credentials_file), args.db, args.days, args.workers, args.rate)
    elif args.command == "minute":
        scan_minutes(client_from_file(args.credentials_file), args.db, args.phase, args.top, args.workers, args.rate)
    elif args.command == "premarket":
        scan_premarket(client_from_file(args.credentials_file), args.db, args.workers, args.rate)
    elif args.command == "export":
        export_minutes(args.db, args.output, args.phase, args.top)
    elif args.command == "report":
        run_phase_report(args.db, args.phase, args.output, args.top)
    elif args.command == "study":
        run_liquidity_study(args.db, args.phase, args.output, args.top)
    elif args.command == "cost-study":
        run_cost_study(args.db, args.output, args.top)
    elif args.command == "publish":
        summary = publish_aggregate_summary(args.db, args.output)
        print(f"published aggregate counts for {summary['session_count']} sessions to {args.output}")
    else:
        result = selection_summary(args.db, args.top)
        print(f"sessions={len(result['sessions'])} winners={len(result['winners'])} prior_top_pairs={sum(len(x['symbols']) for x in result['top_prior'])}")


if __name__ == "__main__":
    main()
