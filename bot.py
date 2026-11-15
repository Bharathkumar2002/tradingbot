"""Intraday moving-average crossover bot for an Alpaca paper account.

Each check downloads recent 5-minute price bars from Alpaca, compares the
20-bar and 50-bar moving averages, and places an order if they just crossed,
up to a daily limit of trades.

Usage:
    python bot.py --dry-run              # show the current signal, never trade
    python bot.py --market-hours-only    # one check; skip if market is closed
    python bot.py --loop                 # keep checking every bar while open
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from dotenv import load_dotenv

from strategy import Signal, find_recent_crossover

ROOT = Path(__file__).resolve().parent
MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN, MARKET_CLOSE = time(9, 30), time(16, 0)
# Every order the bot places gets this prefix, so its trades can be counted.
ORDER_PREFIX = "macross-"
# Orders that never executed don't count toward the daily limit.
NOT_A_TRADE = {OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}

log = logging.getLogger("tradingbot")


# --------------------------------------------------------------------------
# Config and clients
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    symbol: str
    bar_minutes: int
    short_window: int
    long_window: int
    trade_qty: int
    max_trades_per_day: int
    lookback_bars: int
    signal_window_bars: int


def load_config(path: Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    try:
        cfg = Config(
            symbol=str(raw["symbol"]).upper().strip(),
            bar_minutes=int(raw.get("bar_minutes", 5)),
            short_window=int(raw["short_window"]),
            long_window=int(raw["long_window"]),
            trade_qty=int(raw["trade_qty"]),
            max_trades_per_day=int(raw.get("max_trades_per_day", 3)),
            lookback_bars=int(raw.get("lookback_bars", 100)),
            signal_window_bars=int(raw.get("signal_window_bars", 3)),
        )
    except KeyError as e:
        raise SystemExit(f"config error: missing setting {e} in {path}")

    problems = []
    if not 1 <= cfg.bar_minutes <= 59:
        problems.append("bar_minutes must be between 1 and 59")
    if cfg.short_window < 1:
        problems.append("short_window must be at least 1")
    if cfg.short_window >= cfg.long_window:
        problems.append("short_window must be smaller than long_window")
    if cfg.trade_qty < 1:
        problems.append("trade_qty must be at least 1")
    if cfg.max_trades_per_day < 1:
        problems.append("max_trades_per_day must be at least 1")
    if cfg.lookback_bars < cfg.long_window + 1:
        problems.append("lookback_bars must be at least long_window + 1")
    if cfg.signal_window_bars < 1:
        problems.append("signal_window_bars must be at least 1")
    if problems:
        raise SystemExit("config error: " + "; ".join(problems))
    return cfg


def load_keys(allow_live: bool) -> tuple[str, str, bool]:
    load_dotenv(ROOT / ".env")
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(
            "Missing Alpaca keys. Copy .env.example to .env and fill in "
            "ALPACA_API_KEY and ALPACA_SECRET_KEY."
        )
    paper = os.getenv("ALPACA_PAPER", "true").strip().lower() != "false"
    if not paper and not allow_live:
        raise SystemExit(
            "ALPACA_PAPER=false means LIVE trading with real money. "
            "Re-run with --allow-live if you really mean it."
        )
    return key, secret, paper


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------
def fetch_closes(cfg: Config, data: StockHistoricalDataClient) -> pd.Series:
    """Closing prices of the latest completed bars during regular hours (ET)."""
    now = datetime.now(timezone.utc)
    request = StockBarsRequest(
        symbol_or_symbols=cfg.symbol,
        timeframe=TimeFrame(cfg.bar_minutes, TimeFrameUnit.Minute),
        start=now - timedelta(days=7),  # covers weekends and holidays
        end=now,
        feed=DataFeed.IEX,  # free, real-time feed
    )
    df = data.get_stock_bars(request).df
    if df.empty:
        raise RuntimeError(f"Alpaca returned no price bars for {cfg.symbol}")
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(cfg.symbol, level="symbol")

    closes = pd.Series(df["close"].to_numpy(), index=df.index.tz_convert(MARKET_TZ))

    # Regular trading hours only, and only bars that have finished.
    bar_times = closes.index.time
    closes = closes[(bar_times >= MARKET_OPEN) & (bar_times < MARKET_CLOSE)]
    closes = closes[closes.index + timedelta(minutes=cfg.bar_minutes) <= now]

    closes.index = closes.index.tz_localize(None)  # ET wall-clock times
    return closes.tail(cfg.lookback_bars)


# --------------------------------------------------------------------------
# Trading
# --------------------------------------------------------------------------
def held_qty(client: TradingClient, symbol: str) -> float:
    try:
        return float(client.get_open_position(symbol).qty)
    except APIError as e:
        if getattr(e, "status_code", None) == 404:
            return 0.0
        raise


def has_open_order(client: TradingClient, symbol: str, side: OrderSide) -> bool:
    orders = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
    )
    return any(o.side == side for o in orders)


def trades_today(client: TradingClient, symbol: str) -> int:
    """Orders this bot placed today (ET) that weren't canceled or rejected."""
    midnight = datetime.now(MARKET_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    orders = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.ALL, after=midnight, symbols=[symbol], limit=500)
    )
    return sum(
        1
        for o in orders
        if (o.client_order_id or "").startswith(ORDER_PREFIX) and o.status not in NOT_A_TRADE
    )


def act_on_signal(client: TradingClient, cfg: Config, signal: Signal) -> None:
    if signal.action is None:
        log.info("Nothing to do.")
        return

    used = trades_today(client, cfg.symbol)
    if used >= cfg.max_trades_per_day:
        log.info("Daily limit reached (%d of %d trades). Skipping.", used, cfg.max_trades_per_day)
        return

    side = OrderSide.BUY if signal.action == "buy" else OrderSide.SELL
    held = held_qty(client, cfg.symbol)

    if side == OrderSide.BUY:
        if held >= cfg.trade_qty:
            log.info("Buy signal, but already holding %g %s. Skipping.", held, cfg.symbol)
            return
        qty = cfg.trade_qty - int(held)
    else:
        if held <= 0:
            log.info("Sell signal, but no %s shares are held. Skipping (no shorting).", cfg.symbol)
            return
        qty = min(cfg.trade_qty, int(held))

    if has_open_order(client, cfg.symbol, side):
        log.info("An open %s order for %s already exists. Skipping.", side.value, cfg.symbol)
        return

    # One crossover = one order. The id is tied to the crossover's bar, so
    # checking the same crossover again can't place a duplicate.
    client_order_id = f"{ORDER_PREFIX}{cfg.symbol}-{signal.action}-{signal.bar_time:%Y%m%dT%H%M}"
    request = MarketOrderRequest(
        symbol=cfg.symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )
    try:
        order = client.submit_order(request)
    except APIError as e:
        if "client_order_id must be unique" in str(e):
            log.info("Order %s was already placed earlier. Skipping.", client_order_id)
            return
        raise
    log.info(
        "Placed %s order for %s %s (trade %d of %d today): id=%s status=%s",
        side.value.upper(), qty, cfg.symbol, used + 1, cfg.max_trades_per_day,
        order.id, order.status.value,
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def run_once(
    cfg: Config, data: StockHistoricalDataClient, client: TradingClient | None
) -> Signal:
    closes = fetch_closes(cfg, data)
    log.info(
        "%s: %d x %d-min bars, %s to %s | %d-bar vs %d-bar average",
        cfg.symbol, len(closes), cfg.bar_minutes,
        f"{closes.index[0]:%m-%d %H:%M}", f"{closes.index[-1]:%m-%d %H:%M}",
        cfg.short_window, cfg.long_window,
    )
    signal = find_recent_crossover(
        closes, cfg.short_window, cfg.long_window, cfg.signal_window_bars
    )
    log.info(signal.describe())

    if client is None:
        log.info("Dry run: no orders placed.")
    else:
        act_on_signal(client, cfg, signal)
    return signal


def sleep_until(when: datetime) -> None:
    while (remaining := (when - datetime.now(when.tzinfo)).total_seconds()) > 0:
        time_mod.sleep(min(remaining, 600))


def run_forever(cfg: Config, data: StockHistoricalDataClient, client: TradingClient) -> None:
    """Check after every completed bar while the market is open."""
    step = timedelta(minutes=cfg.bar_minutes)
    while True:
        clock = client.get_clock()
        if not clock.is_open:
            # The first bar of the day finishes one bar after the open.
            wake = clock.next_open + step + timedelta(seconds=30)
            log.info("Market closed. Next check at %s", f"{wake.astimezone(MARKET_TZ):%a %Y-%m-%d %H:%M %Z}")
            sleep_until(wake)
            continue
        try:
            run_once(cfg, data, client)
        except Exception:
            log.exception("Check failed; will try again next bar.")
        # Wake 30 s after the next bar finishes.
        now = datetime.now(timezone.utc)
        seconds_into_bar = (now.minute % cfg.bar_minutes) * 60 + now.second
        sleep_until(now + step - timedelta(seconds=seconds_into_bar) + timedelta(seconds=30))


class EasternFormatter(logging.Formatter):
    """Timestamps log lines in New York time, e.g. 2026-11-16 09:35:30 EST."""

    def formatTime(self, record, datefmt=None):
        return f"{datetime.fromtimestamp(record.created, MARKET_TZ):%Y-%m-%d %H:%M:%S %Z}"


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    formatter = EasternFormatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(ROOT / "logs" / "bot.log")]
    for h in handlers:
        h.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Intraday moving-average crossover bot for Alpaca.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="show the signal without trading")
    parser.add_argument("--loop", action="store_true", help="keep running, checking after every bar")
    parser.add_argument(
        "--market-hours-only", action="store_true",
        help="do nothing unless the market is open right now",
    )
    parser.add_argument("--allow-live", action="store_true", help="permit ALPACA_PAPER=false (real money)")
    args = parser.parse_args(argv)
    if args.dry_run and args.loop:
        parser.error("--dry-run and --loop can't be combined")

    setup_logging()
    cfg = load_config(args.config)
    key, secret, paper = load_keys(args.allow_live)
    data = StockHistoricalDataClient(key, secret)

    if args.dry_run:
        run_once(cfg, data, client=None)
        return

    log.info("Connecting to Alpaca (%s account)", "paper" if paper else "LIVE")
    client = TradingClient(key, secret, paper=paper)

    if args.loop:
        run_forever(cfg, data, client)
        return
    if args.market_hours_only and not client.get_clock().is_open:
        log.info("Market is closed right now. Nothing to do.")
        return
    run_once(cfg, data, client)


if __name__ == "__main__":
    main()
