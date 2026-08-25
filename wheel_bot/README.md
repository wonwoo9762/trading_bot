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
wheel_bot/.venv/bin/python -B -m unittest discover -s wheel_bot/tests
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

### Current strategy policy

The cash-secured-put entry is deterministic after the LLM narrows the candidate
universe. The bot scans live put chains for every approved ticker and trades
only when all of these checks pass:

- Expiration is 7-45 calendar days away. There is no target DTE.
- Eligible contracts are ranked by annualized premium after subtracting an
  annualized half-spread cost and a near-expiration gamma penalty. Open interest
  contributes only a small liquidity bonus.
- Delta-proxy POP is 70-85%.
- Gross annualized bid-premium yield on net cash collateral is 20-35%,
  targeting 25%. This is a comparison metric, not a forecast or guarantee of
  portfolio return.
- Open interest is at least 100 and the bid/ask spread is at most 20% of the
  midpoint.
- Quantity is fully cash secured, one underlying is capped at 15% of NLV, and
  total open CSP collateral is capped at 50% of NLV.
- The bot will not start another CSP cycle in an underlying that already has an
  open short put.

The execution LLM cannot change the selected symbol or quantity. The broker
adapter also verifies both fields and requires the submitted limit price to
remain inside the approved bid/ask spread.

### Position lifecycle

Portfolio routing is deterministic; an LLM cannot route around positions that
already exist.

- An open short put routes to `SHORT_PUT_OPEN`, not back to a new CSP entry.
- The bot buys back a short put when the current ask captures at least 50% of
  the original credit.
- Inside 3 DTE, it may buy back after capturing at least 20% to reduce
  near-expiration gamma risk.
- Losing, threatened, stale-quote, and incomplete-data short puts are held and
  reported for manual review. They are not autonomously rolled or liquidated.
- A covered call must be 7-45 DTE, 0.10-0.35 absolute delta, liquid, at least 2%
  above spot, and at or above share cost basis. Existing short calls block a
  duplicate covered call.
- Repeated CRO rejection now ends in `NO_TRADE`/manual review. The graph never
  forces liquidation after an LLM retry loop.

The broker enforces the exact CRO-approved symbol, quantity, order side, and
bid/ask bounds for CSP entries, covered calls, and short-put closes.

The bundled candidate fundamentals/news are explicitly static seed data. Paper
trading may use them for pipeline validation, but the broker blocks new live
CSP orders until `fetch_candidate_universe()` and `fetch_fundamentals()` return
a source beginning with `LIVE_` for the selected ticker
(`candidate_data_live=true`).

The long-run portfolio result will not equal the annualized premium screen.
Assignment losses, missed fills, idle cash, underlying drawdowns, taxes, and
management decisions all affect realized returns. A 20-30% annual portfolio
return is not promised by this policy.

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
