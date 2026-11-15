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
    """Result of checking recent bars for a crossover."""

    action: Optional[Action]  # "buy", "sell", or None when nothing crossed
    bar_time: pd.Timestamp  # bar where the crossover happened (or the latest bar)
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
            f"{what} at bar {self.bar_time:%Y-%m-%d %H:%M} | close={self.close:.2f} | "
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


def _check(closes: pd.Series, short_window: int, long_window: int) -> None:
    if short_window >= long_window:
        raise ValueError("short_window must be smaller than long_window")
    if len(closes) < long_window + 1:
        raise ValueError(
            f"Need at least {long_window + 1} bars to detect a crossover, "
            f"got {len(closes)}"
        )


def _signal_at(frame: pd.DataFrame, i: int) -> Signal:
    """Signal for the step from bar i-1 to bar i."""
    prev, now = frame.iloc[i - 1], frame.iloc[i]
    action: Optional[Action] = None
    if prev.short_ma <= prev.long_ma and now.short_ma > now.long_ma:
        action = "buy"
    elif prev.short_ma >= prev.long_ma and now.short_ma < now.long_ma:
        action = "sell"
    return Signal(
        action=action,
        bar_time=pd.Timestamp(frame.index[i]),
        close=float(now.close),
        short_prev=float(prev.short_ma),
        long_prev=float(prev.long_ma),
        short_now=float(now.short_ma),
        long_now=float(now.long_ma),
    )


def detect_crossover(
    closes: pd.Series, short_window: int, long_window: int
) -> Signal:
    """Check whether the averages crossed between the last two bars.

    A crossover needs two consecutive bars:
      * buy  - previous bar short <= long, latest bar short > long
      * sell - previous bar short >= long, latest bar short < long
    """
    return find_recent_crossover(closes, short_window, long_window, within_bars=1)


def find_recent_crossover(
    closes: pd.Series, short_window: int, long_window: int, within_bars: int
) -> Signal:
    """Return the most recent crossover among the last `within_bars` bars.

    If the averages crossed more than once in that span, only the latest
    crossover counts, since it reflects where the averages are now. If they
    didn't cross at all, the returned signal has action=None and describes
    the latest bar.
    """
    _check(closes, short_window, long_window)
    if within_bars < 1:
        raise ValueError("within_bars must be at least 1")

    frame = add_moving_averages(closes, short_window, long_window)
    last = len(frame) - 1
    first = max(long_window, last - within_bars + 1)
    for i in range(last, first - 1, -1):
        signal = _signal_at(frame, i)
        if signal.action is not None:
            return signal
    return _signal_at(frame, last)
