# Wheel Bot

## Why deployment did not trade

`RunAtLoad` in the macOS plist starts the scheduler process; it does not run a trading cycle by itself. Before this change the process waited until the next 09:45 or 15:30 ET cron time. Also, the package `main.py` only printed a hello message, so deployments that ran the project entrypoint never reached the scheduler.

The broker path also had an execution gate: options orders were hard-coded as `DRY_RUN`. Now the scheduler can run once on startup and can submit CRO-approved Alpaca orders when explicitly enabled.

## Run once

```bash
cd /Users/wonwoochoi/Desktop/trading_bot/wheel_bot
uv run python scheduler.py --once
```

Dry-run data fetch without LLM/order execution:

```bash
uv run python scheduler.py --once --dry-run
```

## Run tests

```bash
cd /Users/wonwoochoi/Desktop/trading_bot
python3 -B -m unittest discover -s wheel_bot/tests
```

## Daemon behavior

The LaunchAgent now runs through `uv` and passes `--run-on-start`, so loading it creates/uses the project environment, triggers one immediate run, and then keeps the normal 09:45 and 15:30 ET schedule.

## Email behavior

Every scheduler trigger sends a report when SMTP is configured. If no order was submitted, the email says "No transaction made" and gives the skip, failure, gate, CRO, or broker reason. If an order was submitted, the email says "Transaction made" and includes the action, symbol, order ID, CRO approval reason, and broker submission reason.

## CASH path

When the account has cash and no stock position, the bot routes to the CASH path and tries to draft a cash-secured put. The flow is:

```text
Macro Sentinel -> Orchestrator -> Candidate Selector -> Fundamental Screener -> Options Chain Fetch -> Put Drafter -> CRO -> Execution Broker
```

Macro Sentinel is a market-wide risk gate. It halts trading during systemic risk conditions, but it does not choose tickers.

Candidate Selector is the node that looks at the candidate universe, local risk/news notes, macro context, and fundamentals to choose tickers worth screening. The current candidate universe is a static seed in `data_feeds.py`; replace `fetch_candidate_universe()` with a live fundamentals/news provider when available. You can override the seed ticker list with:

```env
WHEEL_BOT_CANDIDATE_TICKERS=AAPL,MSFT,GOOGL
```

## Enable Alpaca order submission

Keep paper trading on first:

```env
ALPACA_PAPER_TRADE=True
WHEEL_BOT_AUTO_EXECUTE=True
```

Live trading requires a second explicit opt-in:

```env
ALPACA_PAPER_TRADE=False
WHEEL_BOT_AUTO_EXECUTE=True
WHEEL_BOT_ALLOW_LIVE_TRADING=True
```

The scheduler only submits when the graph produces a CRO-approved ticket and an execution broker output with concrete order fields. Missing spreads, missing contract symbols, LLM failures, data-gate failures, or CRO rejection are reported as blocked rather than submitted.
