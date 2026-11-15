# Intraday moving-average crossover trading bot

Watches one stock all day on an **Alpaca paper account** and trades on a
moving-average crossover of **5-minute price bars**:

- **Buy** when the 20-bar average (last 100 minutes) crosses **above** the
  50-bar average (last ~4 hours).
- **Sell** when the 20-bar average crosses **below** the 50-bar average.
- **At most 3 trades a day.** Buys and sells both count.

A crossover needs two consecutive bars. On one bar the short average is on
one side of the long average, and on the next bar it's on the other side.

Prices come from Alpaca's free real-time IEX feed, using the same keys as
trading.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then put your Alpaca paper keys in .env
```

`.env` is gitignored, so your keys stay on your machine.

## Settings: `config.yaml`

| Setting | Default | Meaning |
|---|---|---|
| `symbol` | `AAPL` | Stock to trade |
| `bar_minutes` | `5` | Size of each price bar |
| `short_window` | `20` | Fast moving average, in bars |
| `long_window` | `50` | Slow moving average, in bars |
| `trade_qty` | `1` | Shares per trade (and the most the bot will hold) |
| `max_trades_per_day` | `3` | Daily order limit |
| `lookback_bars` | `100` | Bars of history downloaded each check |
| `signal_window_bars` | `3` | Act on a crossover from up to this many bars ago |

## Automatic monitoring (GitHub Actions)

`.github/workflows/market-monitor.yml` runs a check about every 5 minutes on
weekdays, on GitHub's servers. Your computer and Claude don't need to be on.
Outside market hours and on holidays each check just stops.

**One-time setup:** add your Alpaca keys as repository secrets.
GitHub → this repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**: `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.

**Checking results:**
- **`bot-logs` branch → `logs/`:** one file per trading day with every check
  made during market hours, every order placed, and any errors.
- **Actions tab → Market monitor:** each run's page shows that check's log.
- **Run a check now:** Actions → Market monitor → **Run workflow**.

GitHub doesn't guarantee schedule timing. When it's busy, checks can start
late or be skipped, so the real gap is often 5–15 minutes. The bot acts on
any crossover from the last 3 bars (15 minutes), so a late check still
catches it. Each crossover can only ever produce one order.

## Running it yourself

```bash
python bot.py --dry-run             # show the current signal; never trades
python bot.py --market-hours-only   # one check; does nothing if market closed
python bot.py --loop                # check after every bar, all day, every day
```

Everything is logged to the screen and to `logs/bot.log`, timestamped in
New York time.

## How it behaves

- **Only finished bars count.** The bar that's still forming is ignored.
- **Market orders.** They fill within seconds during market hours.
- **Holds at most `trade_qty` shares.** A buy signal while already holding is
  skipped. A sell signal sells what it holds, never more, so it never
  short-sells.
- **Daily limit.** The bot counts its own orders placed today (those whose ID
  starts with `macross-`, excluding canceled or rejected ones). Once it
  reaches `max_trades_per_day`, it logs the signal and skips it.
- **No duplicate orders.** Each order's ID is tied to the bar where the
  crossover happened, so checking the same crossover twice places one order.
- **Positions are held overnight.** The bot doesn't sell at the close.
- **Paper trading only by default.** `ALPACA_PAPER=false` (real money) is
  refused unless you also pass `--allow-live`.

## Tests

```bash
pytest
```
