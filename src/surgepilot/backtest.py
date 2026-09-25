"""Minute-bar replay. Signals at close, entries at the next observed open."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN
from itertools import groupby
from zoneinfo import ZoneInfo

from .data import Bar
from .strategy import StrategyConfig, rise_percent, stop_reason

NY = ZoneInfo("America/New_York")
ZERO = Decimal("0")


@dataclass
class Position:
    symbol: str
    quantity: int
    entry_price: Decimal
    entry_time: datetime
    entry_fee: Decimal
    peak: Decimal
    last_close: Decimal
    last_time: datetime


def _money(value: Decimal) -> float:
    return round(float(value), 4)


def run_backtest(bars: list[Bar], config: StrategyConfig, initial_cash: Decimal = Decimal("10000")) -> dict:
    if not bars:
        raise ValueError("empty bar set")
    if initial_cash <= 0:
        raise ValueError("initial cash must be positive")
    bars = sorted(bars, key=lambda b: (b.timestamp, b.symbol))
    cash = initial_cash
    positions: dict[str, Position] = {}
    pending: dict[str, dict] = {}
    histories: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    last_prices: dict[str, Decimal] = {}
    trades: list[dict] = []
    signals: list[dict] = []
    equity: list[dict] = []
    day: str | None = None
    day_start_equity = initial_cash
    entries_today = 0
    entered_symbols: set[str] = set()
    fee_rate = config.fee_bps / 10000
    slip_rate = config.slippage_bps / 10000

    def sell(symbol: str, raw_price: Decimal, when: datetime, reason: str):
        nonlocal cash
        pos = positions.pop(symbol)
        fill = raw_price * (1 - slip_rate)
        proceeds = fill * pos.quantity
        exit_fee = proceeds * fee_rate
        cash += proceeds - exit_fee
        pnl = proceeds - exit_fee - (pos.entry_price * pos.quantity + pos.entry_fee)
        trades.append({"symbol": symbol, "entry_time": pos.entry_time.isoformat(), "exit_time": when.isoformat(),
                       "entry_price": _money(pos.entry_price), "exit_price": _money(fill), "quantity": pos.quantity,
                       "pnl_usd": _money(pnl), "return_pct": _money(pnl / (pos.entry_price * pos.quantity + pos.entry_fee) * 100),
                       "reason": reason})

    for timestamp, same_time in groupby(bars, key=lambda b: b.timestamp):
        now = timestamp.astimezone(NY)
        if not time(9, 30) <= now.time() < time(16, 0):
            continue
        current_day = now.date().isoformat()
        if current_day != day:
            if day is not None:
                for symbol, pos in list(positions.items()):
                    sell(symbol, pos.last_close, pos.last_time, "missing_cutoff_bar")
                for order in pending.values():
                    signals.append({**order["signal"], "decision": "no_next_bar"})
                previous_close = datetime.combine(datetime.fromisoformat(day).date(), time(16, 0), NY)
                equity.append({"time": previous_close.isoformat(), "equity": _money(cash)})
            day = current_day
            day_start_equity = cash
            entries_today = 0
            entered_symbols.clear()
            histories.clear()
            pending.clear()
            last_prices.clear()
        group = list(same_time)
        group.sort(key=lambda b: b.symbol)
        flat_time = (datetime.combine(now.date(), time(16, 0), NY) - timedelta(minutes=config.flatten_minutes)).time()
        cutoff_time = (datetime.combine(now.date(), time(16, 0), NY) - timedelta(minutes=config.entry_cutoff_minutes)).time()

        # Existing positions are checked against the *previous* peak. The current bar's
        # high is only added afterwards because high/low order within a bar is unknown.
        for bar in group:
            if bar.symbol not in positions:
                continue
            pos = positions[bar.symbol]
            if now.time() >= flat_time:
                sell(bar.symbol, bar.open, timestamp, "end_of_day")
                continue
            hard = pos.entry_price * (1 - config.stop_pct / 100)
            trail = pos.peak * (1 - config.trail_pct / 100)
            threshold = max(hard, trail)
            reason = stop_reason(threshold, pos.entry_price, pos.peak, config)
            if bar.open <= threshold:
                sell(bar.symbol, bar.open, timestamp, reason or "stop")
            elif bar.low <= threshold:
                sell(bar.symbol, threshold, timestamp, reason or "stop")
            else:
                pos.peak = max(pos.peak, bar.high)
                pos.last_close, pos.last_time = bar.close, timestamp

        # A signal observed at the preceding close may fill only at this bar's open.
        for bar in group:
            order = pending.pop(bar.symbol, None)
            if order is None or now.time() >= cutoff_time or bar.symbol in positions:
                continue
            slot = order["slot"]
            allocation = min(config.allocation_pct, Decimal(100) - Decimal(slot) * config.allocation_pct)
            budget = min(cash / (1 + fee_rate), day_start_equity * allocation / 100)
            fill = bar.open * (1 + slip_rate)
            quantity = int((budget / fill).to_integral_value(rounding=ROUND_DOWN))
            if quantity < 1:
                signals.append({**order["signal"], "decision": "insufficient_cash"})
                continue
            cost = fill * quantity
            fee = cost * fee_rate
            cash -= cost + fee
            positions[bar.symbol] = Position(bar.symbol, quantity, fill, timestamp, fee, fill,
                                             bar.close, timestamp)
            entries_today += 1
            entered_symbols.add(bar.symbol)
            signals.append({**order["signal"], "decision": "bought", "fill_time": timestamp.isoformat()})
            # Check the current bar against the entry price, then update the peak.
            pos = positions[bar.symbol]
            threshold = fill * (1 - config.stop_pct / 100)
            if bar.low <= threshold:
                sell(bar.symbol, min(bar.open, threshold), timestamp, "hard_stop")
            else:
                pos.peak = max(fill, bar.high)

        candidates = []
        for bar in group:
            last_prices[bar.symbol] = bar.close
            history = histories[bar.symbol]
            history.append((timestamp, bar.close))
            if (now.time() >= cutoff_time or bar.symbol in entered_symbols or bar.symbol in pending
                    or bar.symbol in positions or bar.close < config.min_price or bar.volume < config.min_bar_volume):
                continue
            rise = rise_percent(history, timestamp, config.lookback_minutes)
            if rise is not None and rise >= config.rise_pct:
                candidates.append((rise, bar))
        candidates.sort(key=lambda item: (-item[0], item[1].symbol))
        for rise, bar in candidates:
            signal = {"time": timestamp.isoformat(), "symbol": bar.symbol, "rise_pct": _money(rise),
                      "price": _money(bar.close)}
            if entries_today + len(pending) >= config.max_entries_per_day:
                signals.append({**signal, "decision": "daily_entry_limit"})
            else:
                pending[bar.symbol] = {"slot": entries_today + len(pending), "signal": signal}

        marked = cash + sum(pos.quantity * last_prices.get(symbol, pos.last_close) for symbol, pos in positions.items())
        equity.append({"time": timestamp.isoformat(), "equity": _money(marked)})

    for symbol, pos in list(positions.items()):
        sell(symbol, pos.last_close, pos.last_time, "missing_cutoff_bar")
    for order in pending.values():
        signals.append({**order["signal"], "decision": "no_next_bar"})
    equity.append({"time": bars[-1].timestamp.isoformat(), "equity": _money(cash)})

    peak = initial_cash
    max_drawdown = ZERO
    for point in equity:
        value = Decimal(str(point["equity"]))
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    wins = sum(t["pnl_usd"] > 0 for t in trades)
    return {"kind": "backtest", "source": "historical_csv", "strategy": config.to_dict(),
            "summary": {"initial_cash_usd": _money(initial_cash), "final_equity_usd": _money(cash),
                        "net_return_pct": _money((cash / initial_cash - 1) * 100),
                        "max_drawdown_pct": _money(max_drawdown), "trades": len(trades),
                        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0,
                        "signals": len(signals)},
            "equity": equity, "trades": trades, "signals": signals,
            "limitations": ["1분봉에서는 분봉 안의 고가·저가 순서와 실제 체결 가능 수량을 알 수 없습니다.",
                            "수수료와 체결 불리함은 가정값이며 호가·잔량을 재생하지 않습니다.",
                            "missing_cutoff_bar가 있다면 장 종료 전 청산 가능성을 검증하지 못한 것입니다."]}
