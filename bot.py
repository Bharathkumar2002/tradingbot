"""Moving-average crossover trading bot for an Alpaca paper account.

Usage:
    python bot.py --dry-run   # show today's signal, place no orders
    python bot.py             # check once and trade if there's a crossover
    python bot.py --loop      # keep running: check after every market close
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from dotenv import load_dotenv

from strategy import Signal, detect_crossover

ROOT = Path(__file__).resolve().parent
MARKET_TZ = ZoneInfo("America/New_York")
# A daily bar is treated as final this long after the 4:00 PM ET close.
BAR_FINAL_AFTER = timedelta(hours=16, minutes=15)
# In --loop mode, run this long after each market close.
RUN_AFTER_CLOSE = timedelta(minutes=15)

log = logging.getLogger("tradingbot")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    symbol: str
    short_window: int
    long_window: int
    trade_qty: int
    lookback_days: int


def load_config(path: Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    try:
        cfg = Config(
            symbol=str(raw["symbol"]).upper().strip(),
            short_window=int(raw["short_window"]),
            long_window=int(raw["long_window"]),
            trade_qty=int(raw["trade_qty"]),
            lookback_days=int(raw.get("lookback_days", 100)),
        )
    except KeyError as e:
        raise SystemExit(f"config error: missing setting {e} in {path}")

    problems = []
    if cfg.short_window < 1:
        problems.append("short_window must be at least 1")
    if cfg.short_window >= cfg.long_window:
        problems.append("short_window must be smaller than long_window")
    if cfg.trade_qty < 1:
        problems.append("trade_qty must be at least 1")
    if cfg.lookback_days < cfg.long_window + 1:
        problems.append("lookback_days must be at least long_window + 1")
    if problems:
        raise SystemExit("config error: " + "; ".join(problems))
    return cfg


def make_trading_client(allow_live: bool) -> TradingClient:
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
    log.info("Connecting to Alpaca (%s account)", "paper" if paper else "LIVE")
    return TradingClient(key, secret, paper=paper)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------
def fetch_closes(symbol: str, lookback_days: int) -> pd.Series:
    """Download the last `lookback_days` completed daily closes from Yahoo."""
    # Ask for extra calendar days to cover weekends and holidays.
    start = datetime.now(MARKET_TZ).date() - timedelta(days=int(lookback_days * 1.6) + 10)
    hist = yf.Ticker(symbol).history(start=start, interval="1d", auto_adjust=True)
    if hist.empty:
        raise RuntimeError(f"Yahoo Finance returned no data for {symbol}")

    closes = hist["Close"].dropna()
    closes.index = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()

    # During the trading day Yahoo includes today's unfinished bar. Drop it so
    # signals are only based on days that have closed.
    now = datetime.now(MARKET_TZ)
    today = pd.Timestamp(now.date())
    since_midnight = now - now.replace(hour=0, minute=0, second=0, microsecond=0)
    if closes.index[-1] == today and since_midnight < BAR_FINAL_AFTER:
        closes = closes.iloc[:-1]

    return closes.tail(lookback_days)


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


def act_on_signal(client: TradingClient, cfg: Config, signal: Signal) -> None:
    if signal.action is None:
        log.info("Nothing to do.")
        return

    side = OrderSide.BUY if signal.action == "buy" else OrderSide.SELL
    qty = cfg.trade_qty

    if side == OrderSide.SELL:
        held = held_qty(client, cfg.symbol)
        if held <= 0:
            log.info("Sell signal, but no %s shares are held. Skipping (no shorting).", cfg.symbol)
            return
        qty = min(qty, int(held))

    if has_open_order(client, cfg.symbol, side):
        log.info("An open %s order for %s already exists. Skipping.", side.value, cfg.symbol)
        return

    # One crossover = one order. The id is tied to the signal date, so running
    # the bot again for the same crossover can't place a duplicate.
    client_order_id = f"macross-{cfg.symbol}-{signal.action}-{signal.date.date()}"
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
        "Placed %s order for %s %s: id=%s status=%s",
        side.value.upper(), qty, cfg.symbol, order.id, order.status.value,
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def run_once(cfg: Config, client: TradingClient | None) -> Signal:
    log.info(
        "Checking %s: %d-day vs %d-day moving average over %d days",
        cfg.symbol, cfg.short_window, cfg.long_window, cfg.lookback_days,
    )
    closes = fetch_closes(cfg.symbol, cfg.lookback_days)
    log.info("Got %d daily closes, %s to %s", len(closes), closes.index[0].date(), closes.index[-1].date())

    signal = detect_crossover(closes, cfg.short_window, cfg.long_window)
    log.info(signal.describe())

    if client is None:
        log.info("Dry run: no orders placed.")
    else:
        act_on_signal(client, cfg, signal)
    return signal


def run_forever(cfg: Config, client: TradingClient) -> None:
    """Check now, then again shortly after every market close."""
    while True:
        try:
            run_once(cfg, client)
        except Exception:
            log.exception("Run failed; will try again at the next close.")

        clock = client.get_clock()
        next_run = clock.next_close + RUN_AFTER_CLOSE
        log.info("Next check at %s", next_run.astimezone(MARKET_TZ).strftime("%a %Y-%m-%d %H:%M %Z"))
        while (remaining := (next_run - datetime.now(next_run.tzinfo)).total_seconds()) > 0:
            time.sleep(min(remaining, 600))


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(ROOT / "logs" / "bot.log"),
        ],
    )
    # yfinance is noisy at INFO.
    logging.getLogger("yfinance").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Moving-average crossover bot for Alpaca.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="show the signal without trading")
    parser.add_argument("--loop", action="store_true", help="keep running, checking after each market close")
    parser.add_argument("--allow-live", action="store_true", help="permit ALPACA_PAPER=false (real money)")
    args = parser.parse_args(argv)

    setup_logging()
    cfg = load_config(args.config)

    if args.dry_run:
        if args.loop:
            parser.error("--dry-run and --loop can't be combined")
        run_once(cfg, client=None)
        return

    client = make_trading_client(args.allow_live)
    if args.loop:
        run_forever(cfg, client)
    else:
        run_once(cfg, client)


if __name__ == "__main__":
    main()
