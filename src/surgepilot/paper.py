"""Local, long-running paper trading using Toss ranking and price snapshots.

This is a candidate-limited scanner, not a full-market tick feed. It never calls
an order endpoint and never reads account details.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo

from .strategy import StrategyConfig, rise_percent, stop_reason
from .toss import TossClient

NY = ZoneInfo("America/New_York")


@dataclass
class PaperPosition:
    quantity: int
    entry: Decimal
    entry_fee: Decimal
    entry_time: datetime
    peak: Decimal


class PaperSession:
    def __init__(self, config: StrategyConfig, cash: Decimal, market_date: str):
        self.config = config
        self.initial_cash = cash
        self.cash = cash
        self.market_date = market_date
        self.positions: dict[str, PaperPosition] = {}
        self.prices: dict[str, Decimal] = {}
        self.history: dict[str, list[tuple[datetime, Decimal]]] = {}
        self.entered: set[str] = set()
        self.alerted: set[str] = set()
        self.signals: list[dict] = []
        self.trades: list[dict] = []
        self.equity: list[dict] = []
        self._slip = config.slippage_bps / 10000
        self._fee = config.fee_bps / 10000

    def update(self, now: datetime, quotes: list[dict], allow_entries: bool, flatten: bool = False):
        candidates = []
        for quote in quotes:
            symbol = quote["symbol"]
            price = Decimal(quote["lastPrice"])
            quote_at = datetime.fromisoformat(quote["timestamp"])
            if price <= 0 or quote_at.tzinfo is None or (now - quote_at).total_seconds() > 30:
                continue
            self.prices[symbol] = price
            series = self.history.setdefault(symbol, [])
            series.append((now, price))
            cutoff = now - timedelta(minutes=self.config.lookback_minutes * 2 + 2)
            while series and series[0][0] < cutoff:
                series.pop(0)
            if symbol in self.positions:
                pos = self.positions[symbol]
                reason = "end_of_day" if flatten else stop_reason(price, pos.entry, pos.peak, self.config)
                if reason:
                    self._sell(symbol, price, now, reason)
                else:
                    pos.peak = max(pos.peak, price)
            if not allow_entries or flatten or symbol in self.entered or symbol in self.positions or price < self.config.min_price:
                continue
            rise = rise_percent(series, now, self.config.lookback_minutes)
            if rise is not None and rise >= self.config.rise_pct and symbol not in self.alerted:
                self.alerted.add(symbol)
                candidates.append((rise, symbol, price))
        candidates.sort(reverse=True)
        for rise, symbol, price in candidates:
            signal = {"time": now.isoformat(), "symbol": symbol, "rise_pct": round(float(rise), 3),
                      "price": float(price)}
            if len(self.entered) >= self.config.max_entries_per_day:
                self.signals.append({**signal, "decision": "daily_entry_limit"})
                continue
            slot = len(self.entered)
            allocation = min(self.config.allocation_pct, Decimal(100) - Decimal(slot) * self.config.allocation_pct)
            fill = price * (1 + self._slip)
            budget = min(self.cash / (1 + self._fee), self.initial_cash * allocation / 100)
            quantity = int((budget / fill).to_integral_value(rounding=ROUND_DOWN))
            if quantity < 1:
                self.signals.append({**signal, "decision": "insufficient_cash"})
                continue
            cost = fill * quantity
            fee = cost * self._fee
            self.cash -= cost + fee
            self.positions[symbol] = PaperPosition(quantity, fill, fee, now, fill)
            self.entered.add(symbol)
            self.signals.append({**signal, "decision": "bought"})
        value = self.cash + sum(pos.quantity * self.prices.get(symbol, pos.entry)
                                for symbol, pos in self.positions.items())
        self.equity.append({"time": now.isoformat(), "equity": round(float(value), 4)})

    def _sell(self, symbol: str, price: Decimal, now: datetime, reason: str):
        pos = self.positions.pop(symbol)
        fill = price * (1 - self._slip)
        proceeds = fill * pos.quantity
        fee = proceeds * self._fee
        self.cash += proceeds - fee
        pnl = proceeds - fee - pos.entry * pos.quantity - pos.entry_fee
        self.trades.append({"symbol": symbol, "entry_time": pos.entry_time.isoformat(), "exit_time": now.isoformat(),
                            "entry_price": round(float(pos.entry), 4), "exit_price": round(float(fill), 4),
                            "quantity": pos.quantity, "pnl_usd": round(float(pnl), 4),
                            "return_pct": round(float(pnl / (pos.entry * pos.quantity + pos.entry_fee) * 100), 4),
                            "reason": reason})

    def report(self) -> dict:
        marked = self.cash + sum(pos.quantity * self.prices.get(symbol, pos.entry)
                                 for symbol, pos in self.positions.items())
        return {"kind": "paper", "source": "toss_live_quotes", "market_date": self.market_date,
                "generated_at": datetime.now(tz=NY).isoformat(), "strategy": self.config.to_dict(),
                "summary": {"initial_cash_usd": float(self.initial_cash), "final_equity_usd": round(float(marked), 4),
                            "net_return_pct": round(float((marked / self.initial_cash - 1) * 100), 4),
                            "trades": len(self.trades), "signals": len(self.signals)},
                "positions": [{"symbol": symbol, "quantity": pos.quantity, "entry_price": float(pos.entry),
                               "last_price": float(self.prices.get(symbol, pos.entry))}
                              for symbol, pos in self.positions.items()],
                "equity": self.equity, "trades": self.trades, "signals": self.signals,
                "limitations": ["감시 대상은 거래량 상위 100개와 당일 상승률 상위 100개의 합집합으로 제한됩니다.",
                                "시세 스냅샷과 가정한 체결 불리함으로 실제 주문 체결을 입증할 수 없습니다.",
                                "미국 정규장 내내 실행 프로세스가 켜져 있어야 합니다."]}


def run_paper(client: TossClient, output: str, cash: Decimal, config: StrategyConfig, poll_seconds: int = 10):
    now = datetime.now(NY)
    market_date = now.date().isoformat()
    market = client.market_day(market_date)
    regular = market.get("regularMarket")
    if not regular:
        raise ValueError("US regular market is closed on this date")
    open_at = datetime.fromisoformat(regular["startTime"])
    close_at = datetime.fromisoformat(regular["endTime"])
    flatten_at = close_at - timedelta(minutes=config.flatten_minutes)
    stop_entries_at = close_at - timedelta(minutes=config.entry_cutoff_minutes)
    eligible = {item["symbol"] for market in ("NASDAQ", "NYSE", "AMEX")
                for item in client.stocks(market)}
    session = PaperSession(config, cash, market_date)
    candidates: set[str] = set()
    last_ranking = 0.0
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(session.report(), ensure_ascii=False, indent=2), encoding="utf-8")
    while True:
        now = datetime.now(NY)
        if now >= close_at:
            break
        if now < open_at:
            time.sleep(min(poll_seconds, max(1, (open_at - now).total_seconds())))
            continue
        if time.monotonic() - last_ranking >= 60:
            selected = set()
            for ranking_type, duration in (("MARKET_TRADING_VOLUME", "realtime"), ("TOP_GAINERS", "1d")):
                response = client.rankings(ranking_type, duration)
                selected.update(item["symbol"] for item in response.get("rankings", [])
                                if item["symbol"] in eligible)
            candidates = selected
            last_ranking = time.monotonic()
        symbols = sorted(session.positions) + sorted(candidates - set(session.positions))[:200 - len(session.positions)]
        if symbols:
            quotes = client.prices(symbols)
            session.update(now, quotes, allow_entries=now < stop_entries_at, flatten=now >= flatten_at)
            target.write_text(json.dumps(session.report(), ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(poll_seconds)
    if session.positions:
        # The last available quote is only a paper fallback; the report exposes it.
        for symbol in list(session.positions):
            session._sell(symbol, session.prices[symbol], datetime.now(NY), "last_quote_fallback")
    target.write_text(json.dumps(session.report(), ensure_ascii=False, indent=2), encoding="utf-8")
    return session.report()
