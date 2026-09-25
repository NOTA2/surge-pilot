"""Command line entry point."""

import argparse
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

from .backtest import run_backtest
from .data import load_bars
from .demo import write_demo
from .paper import PaperSession, run_paper
from .research import run_research
from .strategy import StrategyConfig
from .toss import TossClient, collect_candles


def _write(path: str, payload: dict):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target} ({payload['kind']}, {payload['summary']['trades']} trades)")


def _config(args):
    return replace(StrategyConfig(), lookback_minutes=args.lookback,
                   rise_pct=Decimal(str(args.rise)), trail_pct=Decimal(str(args.trail)),
                   stop_pct=Decimal(str(args.stop)), fee_bps=Decimal(str(args.fee_bps)),
                   slippage_bps=Decimal(str(args.slippage_bps)))


def main():
    parser = argparse.ArgumentParser(prog="surge-pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backtest", "research", "paper"):
        command = sub.add_parser(name)
        command.add_argument("--cash", default="10000", help="USD starting cash")
        command.add_argument("--lookback", type=int, default=5)
        command.add_argument("--rise", type=float, default=8)
        command.add_argument("--trail", type=float, default=7)
        command.add_argument("--stop", type=float, default=5)
        command.add_argument("--fee-bps", type=float, default=10)
        command.add_argument("--slippage-bps", type=float, default=15)
        command.add_argument("--output", default="reports/private/result.json")
        if name != "paper":
            command.add_argument("--data", required=True, help="CSV file or directory")
    sub.add_parser("demo").add_argument("--output", default="docs/report.json")
    sub.add_parser("demo-live").add_argument("--output", default="docs/live-report.json")
    collect = sub.add_parser("collect")
    sources = collect.add_mutually_exclusive_group(required=True)
    sources.add_argument("--symbols", help="comma-separated US symbols")
    sources.add_argument("--symbols-file", help="JSON output from the universe command")
    collect.add_argument("--since", required=True, help="ISO 8601 timestamp with timezone")
    collect.add_argument("--output", default="data/private/candles.csv")
    universe = sub.add_parser("universe")
    universe.add_argument("--output", default="data/private/us-universe.json")
    args = parser.parse_args()

    if args.command == "collect":
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            parser.error("--since needs a timezone offset")
        if args.symbols_file:
            symbols = [item["symbol"] for item in json.loads(Path(args.symbols_file).read_text())["stocks"]]
        else:
            symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
        count = collect_candles(TossClient(), symbols, since, args.output)
        print(f"Collected {count} minute bars into {args.output}")
    elif args.command == "universe":
        client = TossClient()
        stocks = [item for market in ("NASDAQ", "NYSE", "AMEX") for item in client.stocks(market)]
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"generated_at": datetime.now().astimezone().isoformat(),
                                      "markets": ["NASDAQ", "NYSE", "AMEX"], "stocks": stocks},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(stocks)} stocks to {target}")
    elif args.command == "demo":
        with TemporaryDirectory() as directory:
            file = Path(directory) / "synthetic.csv"
            write_demo(file)
            report = run_research(load_bars(file))
        report["source"] = "synthetic_demo"
        report["is_demo"] = True
        report["limitations"].insert(0, "모든 가격은 합성 예제이며 실제 투자 성과의 근거가 아닙니다.")
        _write(args.output, report)
    elif args.command == "demo-live":
        with TemporaryDirectory() as directory:
            file = Path(directory) / "synthetic.csv"
            write_demo(file)
            bars = [bar for bar in load_bars(file) if bar.timestamp.date().isoformat() == "2026-09-22"
                    and bar.timestamp.hour == 9 and bar.timestamp.minute <= 59]
        session = PaperSession(StrategyConfig(), Decimal("10000"), "2026-09-22")
        for timestamp, group in groupby(bars, key=lambda bar: bar.timestamp):
            session.update(timestamp, [{"symbol": bar.symbol, "lastPrice": str(bar.close),
                                        "timestamp": timestamp.isoformat()} for bar in group], True)
        report = session.report()
        report["source"] = "synthetic_demo"
        report["is_demo"] = True
        report["limitations"].insert(0, "모든 가격과 가상 포지션은 합성 예제입니다.")
        _write(args.output, report)
    elif args.command == "paper":
        report = run_paper(TossClient(), args.output, Decimal(args.cash), _config(args))
        print(f"Paper session ended: {report['summary']}")
    else:
        bars = load_bars(args.data)
        report = run_backtest(bars, _config(args), Decimal(args.cash)) if args.command == "backtest" else run_research(bars, Decimal(args.cash))
        _write(args.output, report)


if __name__ == "__main__":
    main()
