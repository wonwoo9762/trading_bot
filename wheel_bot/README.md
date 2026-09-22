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

On Windows, from the repository root:

```powershell
& .\wheel_bot\.venv\Scripts\python.exe -B -m unittest discover -s wheel_bot/tests
```

Install the locked project dependencies first. The suite includes a subprocess
that uses real LangGraph and Alpaca request models with all network connections
blocked. Its market data, agent responses, and broker transport are fixtures.

## Daemon behavior

The LaunchAgent now runs through `uv` and passes `--run-on-start`, so loading it creates/uses the project environment, triggers one immediate run, and then keeps the normal 09:45 and 15:30 ET schedule.

## Email behavior

Every scheduler trigger sends a report when SMTP is configured. Reports distinguish
"No new order submitted", "Order submitted; fill not confirmed", an existing order
found during reconciliation, and an uncertain submission. Order IDs and client
order IDs are included when available. Submission alone is not fill confirmation.

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

An empty or failed candidate selection ends in `NO_TRADE`. The screener cannot
reintroduce excluded candidates, and neither agent may expand the supplied universe.

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

Execution parameters are constructed by code after CRO approval. Code copies
the selected contract and whole-contract quantity, sets explicit `sell_to_open`
or `buy_to_close` intent, and uses a midpoint rounded to cents within the approved
bid/ask spread. It never submits a market order or automatically reprices an
unfilled order. The broker independently rejects changed symbols, quantities,
sides, intent, added legs, invalid numbers, and out-of-bounds prices.

Quotes are still the approved ticket's snapshot: this does not yet verify freshness,
contract-specific tick increments, or refresh collateral and positions at submission.

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
- Automated `ROLL` and `SPREAD` orders are disabled at the strategy, validator,
  scheduler, and broker boundaries. Quant/assessor suggestions remain visible
  for manual review until every leg, position, collateral, and maximum loss can
  be validated. The scheduler cannot submit liquidation orders.

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

## Duplicate orders and uncertain submissions

The broker uses a persistent SQLite journal at `wheel_bot/data/order_journal.sqlite3`.
Each account/environment/action/contract gets one stable client order ID per
Eastern trading date. Changing quantity or price does not create another attempt.
An attempted contract/action cannot be automatically retried that day, even after
cancellation, rejection, or a fill. A new Eastern date permits a new intent only
after previous unresolved submissions have been reconciled.

Before submitting, the broker checks the account identity, reconciles unresolved
journal entries by client order ID, checks for an existing matching broker order,
and requires the account's open-order list to be empty. This conservative gate
includes manual orders, partial fills, pending cancellations, and orders in other
symbols. It also applies to automated closes; urgent conflicts need manual review.
Pending collateral and share coverage are not yet allocated across concurrent trades.

A reservation is committed before submission. Workers using the same journal
cannot both reserve while an earlier submission is unresolved. API lookup errors,
invalid responses, or journal failures block submission. Only HTTP 404 is treated
as an absent broker order; it never clears a previously reserved, uncertain attempt.

After a submission exception, the broker looks up the same client ID without
resubmitting. It reports `RECONCILED` if the broker order is found or `UNKNOWN`
otherwise. An unresolved record survives restarts and date changes. Later attempts
can reconcile an order that becomes visible and terminal. If Alpaca never shows
the order, manual investigation is required; there is intentionally no automatic
timeout that forgets the reservation. Do not delete the journal to bypass this gate.

Keep one deployment per account and preserve this journal across restarts. Separate
hosts/journals and concurrent manual account changes are not covered by the local
reservation lock. These safeguards do not replace complete portfolio reconciliation.

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
