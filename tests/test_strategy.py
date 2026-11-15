import pandas as pd
import pytest

from strategy import detect_crossover, find_recent_crossover


def series(values):
    idx = pd.date_range("2026-11-16 09:30", periods=len(values), freq="5min")
    return pd.Series(values, index=idx, dtype=float)


# With windows 2 and 4 the math is easy to follow by hand.
SHORT, LONG = 2, 4


def test_buy_when_short_crosses_above_long():
    # Bar -2: short=10, long=10 (equal). Bar -1: short=15 > long=12.5.
    s = detect_crossover(series([10, 10, 10, 10, 10, 20]), SHORT, LONG)
    assert s.action == "buy"


def test_sell_when_short_crosses_below_long():
    s = detect_crossover(series([10, 10, 10, 10, 10, 0]), SHORT, LONG)
    assert s.action == "sell"


def test_no_signal_when_already_above_on_both_bars():
    s = detect_crossover(series([1, 2, 3, 4, 5, 6, 7]), SHORT, LONG)
    assert s.action is None
    assert s.short_prev > s.long_prev and s.short_now > s.long_now


def test_no_signal_when_already_below_on_both_bars():
    s = detect_crossover(series([7, 6, 5, 4, 3, 2, 1]), SHORT, LONG)
    assert s.action is None


def test_flat_prices_give_no_signal():
    s = detect_crossover(series([10] * 8), SHORT, LONG)
    assert s.action is None


def test_signal_reports_bar_time_and_close():
    prices = series([10, 10, 10, 10, 10, 20])
    s = detect_crossover(prices, SHORT, LONG)
    assert s.bar_time == prices.index[-1]
    assert s.close == 20


def test_recent_crossover_found_a_few_bars_back():
    # Crosses up at bar index 5, then keeps rising: no cross on the last bar.
    prices = series([10, 10, 10, 10, 10, 20, 21, 22])
    assert detect_crossover(prices, SHORT, LONG).action is None
    s = find_recent_crossover(prices, SHORT, LONG, within_bars=3)
    assert s.action == "buy"
    assert s.bar_time == prices.index[5]


def test_recent_crossover_ignored_when_outside_window():
    prices = series([10, 10, 10, 10, 10, 20, 21, 22, 23, 24])
    s = find_recent_crossover(prices, SHORT, LONG, within_bars=3)
    assert s.action is None
    assert s.bar_time == prices.index[-1]


def test_latest_of_two_crossovers_wins():
    # Up-cross at index 5, then a sharp drop makes a down-cross at index 7.
    prices = series([10, 10, 10, 10, 10, 20, 12, 0])
    s = find_recent_crossover(prices, SHORT, LONG, within_bars=3)
    assert s.action == "sell"
    assert s.bar_time == prices.index[7]


def test_rejects_too_little_history():
    with pytest.raises(ValueError):
        detect_crossover(series([1, 2, 3, 4]), SHORT, LONG)


def test_rejects_short_window_not_smaller():
    with pytest.raises(ValueError):
        detect_crossover(series(range(10)), 4, 4)
