# Moving-average crossover trading bot

Trades one stock on an **Alpaca paper account** using a moving-average
crossover:

- **Buy** when the short average (20-day) crosses **above** the long average (50-day).
- **Sell** when the short average crosses **below** the long average.

A crossover needs two consecutive days. Yesterday the short average was on
one side of the long average, and today it's on the other side. If the
averages were already apart on both days, nothing happens.

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
| `short_window` | `20` | Fast moving average, in trading days |
| `long_window` | `50` | Slow moving average, in trading days |
| `trade_qty` | `1` | Shares per buy or sell |
| `lookback_days` | `100` | Days of history downloaded from Yahoo Finance |

## Running it

```bash
python bot.py --dry-run   # show today's signal; never trades
python bot.py             # check once; place an order if there's a crossover
python bot.py --loop      # keep running; check 15 min after every market close
```

Everything is logged to the screen and to `logs/bot.log`.

`--trading-days-only` makes the bot do nothing on weekends and market holidays.

## Automatic daily runs (GitHub Actions)

`.github/workflows/daily-trade.yml` runs the bot every weekday at **9:30 AM ET**
(the market open) on GitHub's servers. Your computer doesn't need to be on.

**One-time setup:** add your Alpaca keys as repository secrets.
GitHub → this repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**, twice:

| Name | Value |
|---|---|
| `ALPACA_API_KEY` | your Alpaca paper key ID |
| `ALPACA_SECRET_KEY` | your Alpaca paper secret key |

**Checking results:**
- **Actions tab → Daily trade → a run:** the summary page shows that morning's log.
- **`bot-logs` branch → `logs/` folder:** one file per day (`2026-11-16.log`, ...),
  kept permanently.
- **Run it now:** Actions tab → Daily trade → **Run workflow**.

GitHub sometimes starts scheduled runs 5–30 minutes late. The bot uses the
most recent *completed* day's prices, so a late start only changes the fill
price, not the signal.

## How it behaves

- **Only closed days count.** If you run it during market hours, today's
  unfinished price bar is ignored.
- **Orders are market orders, good for the day.** A signal found after the
  close is queued and fills at the next market open.
- **No duplicate orders.** Each order's ID is tied to the crossover date
  (for example `macross-AAPL-buy-2026-11-13`), so running the bot twice for the
  same crossover places only one order. The bot also skips a signal if an
  open order on the same side already exists.
- **No short selling.** On a sell signal the bot sells up to `trade_qty`
  shares you already hold. If you hold none, it does nothing.
- **Paper trading only by default.** Setting `ALPACA_PAPER=false` (real money)
  is refused unless you also pass `--allow-live`.

## Tests

```bash
pytest
```
