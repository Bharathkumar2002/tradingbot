"""Moving-average crossover signal logic.

Kept free of network and broker code so it can be tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

Action = Literal["buy", "sell"]


@dataclass(frozen=True)
class Signal:
    """Result of checking the latest two days for a crossover."""

    action: Optional[Action]  # "buy", "sell", or None when nothing crossed
    date: pd.Timestamp  # date of the most recent completed bar
    close: float
    short_prev: float
    long_prev: float
    short_now: float
    long_now: float

    def describe(self) -> str:
        what = {
            "buy": "BUY: short MA crossed ABOVE long MA",
            "sell": "SELL: short MA crossed BELOW long MA",
            None: "No crossover",
        }[self.action]
        return (
            f"{what} on {self.date.date()} | close={self.close:.2f} | "
            f"short {self.short_prev:.2f} -> {self.short_now:.2f}, "
            f"long {self.long_prev:.2f} -> {self.long_now:.2f}"
        )


def add_moving_averages(
    closes: pd.Series, short_window: int, long_window: int
) -> pd.DataFrame:
    """Return a frame with close, short_ma and long_ma columns."""
    return pd.DataFrame(
        {
            "close": closes,
            "short_ma": closes.rolling(short_window).mean(),
            "long_ma": closes.rolling(long_window).mean(),
        }
    )


def detect_crossover(
    closes: pd.Series, short_window: int, long_window: int
) -> Signal:
    """Compare the last two completed days to see if the averages crossed.

    A crossover needs two consecutive days:
      * buy  - yesterday short <= long, and today short > long
      * sell - yesterday short >= long, and today short < long
    If the averages were already apart on both days, there is no signal.
    """
    if short_window >= long_window:
        raise ValueError("short_window must be smaller than long_window")
    if len(closes) < long_window + 1:
        raise ValueError(
            f"Need at least {long_window + 1} days of prices to detect a "
            f"crossover, got {len(closes)}"
        )

    frame = add_moving_averages(closes, short_window, long_window)
    prev, now = frame.iloc[-2], frame.iloc[-1]

    action: Optional[Action] = None
    if prev.short_ma <= prev.long_ma and now.short_ma > now.long_ma:
        action = "buy"
    elif prev.short_ma >= prev.long_ma and now.short_ma < now.long_ma:
        action = "sell"

    return Signal(
        action=action,
        date=pd.Timestamp(frame.index[-1]),
        close=float(now.close),
        short_prev=float(prev.short_ma),
        long_prev=float(prev.long_ma),
        short_now=float(now.short_ma),
        long_now=float(now.long_ma),
    )
