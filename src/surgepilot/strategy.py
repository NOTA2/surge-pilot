"""Pure momentum signal and exit rules shared by replay and paper trading."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class StrategyConfig:
    lookback_minutes: int = 5
    rise_pct: Decimal = Decimal("8")
    trail_pct: Decimal = Decimal("7")
    stop_pct: Decimal = Decimal("5")
    allocation_pct: Decimal = Decimal("40")
    max_entries_per_day: int = 3
    min_price: Decimal = Decimal("1")
    min_bar_volume: int = 1000
    entry_cutoff_minutes: int = 20
    flatten_minutes: int = 5
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("15")

    def __post_init__(self):
        if self.lookback_minutes < 1 or self.max_entries_per_day < 1:
            raise ValueError("lookback and max entries must be positive")
        for name in ("rise_pct", "trail_pct", "stop_pct", "allocation_pct"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.allocation_pct > 100 or self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("invalid allocation or costs")

    def to_dict(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


def rise_percent(history: list[tuple[datetime, Decimal]], now: datetime, lookback_minutes: int) -> Decimal | None:
    """Use the most recent observation at/before t-N, never a future observation."""
    cutoff = now - timedelta(minutes=lookback_minutes)
    earlier = next((price for time, price in reversed(history) if time <= cutoff), None)
    if earlier is None or earlier <= 0:
        return None
    return (history[-1][1] / earlier - 1) * 100


def stop_reason(price: Decimal, entry: Decimal, peak: Decimal, config: StrategyConfig) -> str | None:
    if price <= entry * (1 - config.stop_pct / 100):
        return "hard_stop"
    if price <= peak * (1 - config.trail_pct / 100):
        return "trailing_stop"
    return None
