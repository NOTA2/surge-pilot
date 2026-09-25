"""Chronological parameter search and event/false-positive diagnostics."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from itertools import product
from random import Random
from zoneinfo import ZoneInfo

from .backtest import run_backtest
from .data import Bar
from .strategy import StrategyConfig

NY = ZoneInfo("America/New_York")


def _day(bar: Bar) -> str:
    return bar.timestamp.astimezone(NY).date().isoformat()


def _event_stats(bars: list[Bar], report: dict) -> dict:
    by_pair: dict[tuple[str, str], list[Bar]] = {}
    for bar in bars:
        by_pair.setdefault((_day(bar), bar.symbol), []).append(bar)
    winners = set()
    threshold_times = {}
    for pair, group in by_pair.items():
        group.sort(key=lambda b: b.timestamp)
        base = group[0].open
        threshold = base * Decimal("1.5")
        hit = next((bar.timestamp for bar in group if bar.high >= threshold), None)
        if hit:
            winners.add(pair)
            threshold_times[pair] = hit
    caught = set()
    for signal in report["signals"]:
        pair = (datetime.fromisoformat(signal["time"]).astimezone(NY).date().isoformat(), signal["symbol"])
        if pair in winners and datetime.fromisoformat(signal["time"]) < threshold_times[pair]:
            caught.add(pair)
    return {"symbol_days": len(by_pair), "days_with_50pct_rise": len(winners),
            "caught_before_50pct": len(caught),
            "event_recall_pct": round(len(caught) / len(winners) * 100, 2) if winners else None,
            "note": "50% is measured from the first regular-session bar open; used only after signals are generated."}


def run_research(bars: list[Bar], initial_cash: Decimal = Decimal("10000"), seed: int = 17) -> dict:
    days = sorted({_day(bar) for bar in bars})
    if len(days) < 4:
        raise ValueError("research needs at least four US trading days")
    split = max(2, int(len(days) * .7))
    if split >= len(days):
        split = len(days) - 1
    training = [bar for bar in bars if _day(bar) in set(days[:split])]
    holdout = [bar for bar in bars if _day(bar) in set(days[split:])]
    base = StrategyConfig()
    candidates = []
    for n, m, x, y in product((3, 5, 10), (5, 8, 12), (5, 7), (4, 6)):
        config = replace(base, lookback_minutes=n, rise_pct=Decimal(m), trail_pct=Decimal(x), stop_pct=Decimal(y))
        result = run_backtest(training, config, initial_cash)
        summary = result["summary"]
        score = summary["net_return_pct"] - summary["max_drawdown_pct"]
        # A no-trade strategy must not win merely by avoiding risk.
        if summary["trades"] == 0:
            score = -1e9
        candidates.append((score, config, summary))
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, chosen, train_summary = candidates[0]
    holdout_report = run_backtest(holdout, chosen, initial_cash)
    rng = Random(seed)
    pairs = sorted({(_day(bar), bar.symbol) for bar in holdout})
    sampled = set(rng.sample(pairs, max(1, len(pairs) // 4)))
    random_bars = [bar for bar in holdout if (_day(bar), bar.symbol) in sampled]
    random_report = run_backtest(random_bars, chosen, initial_cash)
    holdout_report.update({"kind": "research", "source": "historical_csv", "chosen_on": "training_only",
                           "train_dates": [days[0], days[split - 1]], "holdout_dates": [days[split], days[-1]],
                           "train_summary": train_summary, "event_analysis": _event_stats(holdout, holdout_report),
                           "random_control": {"seed": seed, "symbol_days": len(sampled),
                                              "summary": random_report["summary"]},
                           "search": {"candidates": len(candidates), "score": "training net return minus max drawdown",
                                      "top": [{"strategy": config.to_dict(), "score": round(score, 3), "summary": summary}
                                              for score, config, summary in candidates[:5]]}})
    return holdout_report
