import pandas as pd
import pytest

from strategy import detect_crossover


def series(values):
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


# With windows 2 and 4 the math is easy to follow by hand.
SHORT, LONG = 2, 4


def test_buy_when_short_crosses_above_long():
    # Day -2: short=10, long=10 (equal). Day -1: short=15 > long=12.5.
    s = detect_crossover(series([10, 10, 10, 10, 10, 20]), SHORT, LONG)
    assert s.action == "buy"


def test_sell_when_short_crosses_below_long():
    s = detect_crossover(series([10, 10, 10, 10, 10, 0]), SHORT, LONG)
    assert s.action == "sell"


def test_no_signal_when_already_above_on_both_days():
    # Steady uptrend: short stays above long, so no new crossover.
    s = detect_crossover(series([1, 2, 3, 4, 5, 6, 7]), SHORT, LONG)
    assert s.action is None
    assert s.short_prev > s.long_prev and s.short_now > s.long_now


def test_no_signal_when_already_below_on_both_days():
    s = detect_crossover(series([7, 6, 5, 4, 3, 2, 1]), SHORT, LONG)
    assert s.action is None


def test_flat_prices_give_no_signal():
    s = detect_crossover(series([10] * 8), SHORT, LONG)
    assert s.action is None


def test_signal_reports_last_bar_date_and_close():
    prices = series([10, 10, 10, 10, 10, 20])
    s = detect_crossover(prices, SHORT, LONG)
    assert s.date == prices.index[-1]
    assert s.close == 20


def test_rejects_too_little_history():
    with pytest.raises(ValueError):
        detect_crossover(series([1, 2, 3, 4]), SHORT, LONG)


def test_rejects_short_window_not_smaller():
    with pytest.raises(ValueError):
        detect_crossover(series(range(10)), 4, 4)
