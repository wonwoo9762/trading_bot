**Wheel Bot: project, strategy, and live-readiness review**

**Implementation update:** This report and its captured reproduction results describe
the original `6c79945` snapshot. Subsequent user-authorized execution, candidate-veto,
and duplicate-order fixes are tracked in [IMPLEMENTATION_NOTES.md](C:/Users/WonwooChoi/trading_bot/review/IMPLEMENTATION_NOTES.md).
Original line references and the historical reproduction script are not current regression tests.

Reviewed repository commit: `6c79945`. Review date: September 21, 2026, Pacific time. Account context supplied by the owner: paper trading, $1 million cash, target of 30% before tax and 20–25% after tax. A maximum acceptable drawdown has not yet been specified.

**My assessment: keep this in paper trading.** The project has a useful foundation for controlled experimentation, but it does not yet establish a profitable strategy or provide the execution and portfolio controls I would require before putting a $1 million account behind it. Several protections are real and worth preserving. Others exist only in prompts, consume placeholder information, or can be bypassed by a valid model response. Upgrading the model alone would leave these problems intact.

I reviewed the application modules, agent graph, prompts, schemas, broker adapter, scheduler, notification code, helper scripts, deployment files, dependency declarations and lockfile, README, and all ten test modules. I installed the locked dependencies in the ignored local virtual environment and ran the existing suite with socket connections blocked: **70 tests passed**. I also exercised the real installed LangGraph engine and Alpaca request models with fake external services. The reproductions and captured results are in [offline_review_checks.py](C:/Users/WonwooChoi/trading_bot/review/offline_review_checks.py) and [offline_review_results.json](C:/Users/WonwooChoi/trading_bot/review/offline_review_results.json).

No trading, LLM, or email service was called during the checks. A fake broker returning `SUBMITTED` proves the application reaches its submission method; it does not prove Alpaca would accept or fill that order. There was no paper performance history or trading database in the supplied checkout to validate realized returns. The account value is owner-supplied, not independently verified. Application source, trading settings, and the lockfile were left unchanged; additions are confined to this review directory.

**The return objective needs to be separated from the premium screen.**

The implemented entry metric is:

`annualized premium yield = bid / (strike - bid) × 365 / days_to_expiration`

This annualizes the initial premium relative to net collateral. It does not deduct future option repurchases, assignment losses, stock losses, transaction costs, or taxes. The code requires 20–35%; the README mentions a 25% target, but selection actually maximizes a heuristic score within the band. There is no objective function targeting 30% total account return. See [entry selection](C:/Users/WonwooChoi/trading_bot/wheel_bot/agents/graph.py:449).

For a rough scale illustration, assuming constant collateral utilization, repeatable premiums, and holding periods matching the quoted DTE:

| Deployment assumption | Collateral on $1 million | Approximate annual premium scale at the 20–35% screen |
|---|---:|---:|
| One underlying at the 15% cap | $150,000 | $30,000–$52,500, or 3–5.25% of account |
| Intended 50% aggregate CSP cap | $500,000 | $100,000–$175,000, or 10–17.5% of account |
| Your before-tax target | — | $300,000, or 30% of account |

These figures are arithmetic illustrations, **not forecasts, backtests, or strict total-return ceilings**. They slightly overstate premium relative to gross strike collateral because the screen uses net collateral. Early closes change turnover and require buybacks; assignments change exposure; stock appreciation and cash interest contribute separately. For a single tenor, the exact gross-collateral premium rate is `Y / (1 + Y × DTE / 365)`, where `Y` is the screen rate as a decimal. Simply multiplying a 25% option yield by the entire account value is not valid.

The current routing problem also means the normal all-cash workflow tends to remain at one active underlying, discussed below. Raising concentration or leverage to close the return gap would increase risk without establishing an edge.

A useful external reference is Cboe's collateralized S&P 500 PutWrite benchmark. Its August 31, 2026 factsheet reports a 7.1% annualized return and a -32.7% maximum drawdown over its January 2007 onward measurement period, using month-end values. It uses monthly at-the-money index puts and Treasury bills, so it is not a forecast for this single-stock wheel. It does illustrate why premium collection and safe annual returns must be evaluated separately. [Cboe PUT factsheet, page 2](https://cdn.cboe.com/resources/indices/factsheet/CboeGlobalIndices_PUT-Index.pdf).

Cash security funds the assignment obligation; it does not prevent investment losses. As a hypothetical example, $500,000 of puts sold at a $100 strike, collecting $2 per share, loses $140,000 at expiration if all underlyings end at $70: $150,000 intrinsic loss less $10,000 premium. That is 14% of the original account. This is a stress example, not a probability estimate. The Options Industry Council describes the substantial downside of cash-secured puts and the capped upside and substantial stock downside of covered calls. [Cash-secured puts](https://www.optionseducation.org/strategies/all-strategies/cash-secured-put), [covered calls](https://www.optionseducation.org/strategies/all-strategies/covered-call-buy-write).

**The most urgent execution findings follow. P1 means resolve before live deployment; P2 means material correctness or operational work.**

**1. P1 — A multi-leg response bypasses the approved single-leg trade. Confirmed.**

In [broker.py:222](C:/Users/WonwooChoi/trading_bot/wheel_bot/broker.py:222), any nonempty `params.legs` goes directly to the multi-leg submission path. The symbol, quantity, side, and price checks for CSPs, covered calls, and short-put closes occur later and are skipped. `BrokerOutput` permits legs regardless of the approved action.

The offline reproduction approved one AAPL put, then supplied a schema-valid execution response for a 50-contract MSFT put spread. Real Alpaca request construction succeeded and the fake broker received the substituted order. Broker-level options approval may reject some exposures, but account buying-power rules do not enforce your strategy's exact approval.

Change: reject legs for every single-leg action; construct orders deterministically from a typed approved ticket. For genuine multi-leg orders, match every symbol, side, intent, ratio, quantity, and price constraint to that ticket. Bind approval to a ticket hash, account snapshot version, and expiration time. A language model should never synthesize new execution authority after risk approval.

**2. P1 — The distressed repair branch lacks enforceable trade and risk validation. Confirmed.**

[QuantOutput](C:/Users/WonwooChoi/trading_bot/wheel_bot/models.py:56) permits `ROLL` with no legs, no quantity, no contract symbols, and no credit. Credit units are explicitly ambiguous: per share or total. [The validator](C:/Users/WonwooChoi/trading_bot/wheel_bot/agents/graph.py:1464) accepts quant-originated tickets as long as an action exists. The offline check passed a roll with no legs and negative estimated credit through this validator.

The CRO prompt exempts rolls and spreads from most constraints and characterizes rolls as risk-reducing. That is not generally true: a roll can extend time at risk, increase quantity, change strikes, or add exposure. The nominal ticket contains portfolio context; the forwarded quant ticket does not preserve that complete context. The execution model is left to infer executable details from a broad chain.

Change: keep automated repair disabled until code verifies owned closing legs, actual available quantities, option type, expirations, coverage, maximum loss, cash requirements, aggregate exposure, and worst permitted execution price. Calculate net credit from quotes with explicit units and fees. Compare holding, closing, and rolling on economic risk rather than requiring credit at all costs. A credit does not erase the loss realized on the closed leg.

**3. P1 — Pending orders and retries are not reconciled. Confirmed.**

[Portfolio loading](C:/Users/WonwooChoi/trading_bot/wheel_bot/data_feeds.py:43) reads account and filled positions, but not open orders. [Order IDs](C:/Users/WonwooChoi/trading_bot/wheel_bot/broker.py:467) are newly randomized for every attempt. Repeating the same ticket twice produced two submission calls with different client IDs.

An unfilled morning order, a restart, an overlapping manual run, or a timeout after broker acceptance can therefore produce a second intent against the same cash or shares. The process has no persistent order reservation, account-wide execution lock, or state for an uncertain submission. Single-leg requests also omit explicit opening/closing intent, as the SDK inspection confirmed. A repeated close must not become an unintended opening purchase.

Change: write a durable intent before submission; reserve cash/shares for outstanding quantities; use one stable client ID per intent; query the broker after ambiguous responses before deciding whether to retry. Reconcile accepted, partially filled, filled, canceled, expired, and rejected orders. Revalidate the full portfolio immediately before submission under a shared account lock. Set explicit position intent. Alpaca documents lookup by client order ID and streaming order updates. [Working with orders](https://docs.alpaca.markets/us/docs/working-with-orders).

**4. P1 — Quote freshness, source, and executable size are missing. Confirmed.**

[Chain serialization](C:/Users/WonwooChoi/trading_bot/wheel_bot/data_feeds.py:475) discards quote timestamps and sizes. The selector sees only prices, delta, open interest, and a few other values. The broker compares against the same old spread rather than a refreshed quote. A mocked quote dated in the year 2000 still produced `SELL_CSP` after passing through the real data transformation and deterministic selector.

Requests do not specify an options feed. The installed SDK describes a subscription-dependent default; a returned quote should not automatically be considered a live executable OPRA quote. Alpaca distinguishes its indicative derivative feed from OPRA. [Historical option data and feeds](https://docs.alpaca.markets/us/docs/historical-option-data).

Change: retain typed quote timestamps, receipt times, feed, sizes, exchange/session status, and contract metadata. Require fresh eligible quotes for new risk, specify and verify the intended feed, and reprice/revalidate before submission. Check minimum tick increments. At $1 million, size relative to quoted liquidity matters; `open_interest >= 100` alone does not establish that a multi-contract order can fill near midpoint.

**5. P1 — Macro and company information are placeholders, with an unsafe missing-data policy. Confirmed by source.**

[fetch_macro](C:/Users/WonwooChoi/trading_bot/wheel_bot/data_feeds.py:298) returns unavailable VIX/FOMC data while stating that no breaking events were detected, despite fetching no news. [The macro prompt](C:/Users/WonwooChoi/trading_bot/wheel_bot/prompts.py:73) allows missing indicators to default to CLEAR. A current timestamp describes when the placeholder was generated, not when market facts were observed.

Fundamentals and company news are hardcoded for five stocks. The four-quarter positive-cash-flow rule cannot be verified from the single `fcf` number supplied. The live CSP block is a valuable safeguard, but `candidate_data_live` is derived only from whether the selected fundamental row's source starts with `LIVE_`; it does not verify freshness, provenance, or both candidate and fundamental sources as the README implies. Other order paths do not receive the equivalent live-data gate.

Change: integrate dated, attributable data; represent missing/unknown explicitly; make numeric macro limits deterministic. Unknown critical inputs should prevent new risk, while a separate process continues position observation and approved risk reduction. Require actual publication/as-of timestamps and quality checks, not a source-name prefix. Do not replace static numbers with newer static numbers and call the result live.

**The portfolio and strategy need changes beyond execution safety.**

**6. P1 — One account-wide state prevents the intended diversification and neglects holdings. Confirmed.**

[Routing](C:/Users/WonwooChoi/trading_bot/wheel_bot/agents/graph.py:209) sends the whole account to `SHORT_PUT_OPEN` if any put exists. New candidate screening is then unreachable. Any stock holding similarly diverts the workflow away from new CSP entries. [fetch_portfolio](C:/Users/WonwooChoi/trading_bot/wheel_bot/data_feeds.py:49) keeps only the largest equity holding in the strategy state, although it includes all short puts and calls.

With a synthetic $1 million cash account, the first ticket used $150,000 of collateral. On the next run, the real graph visited only Macro and Orchestrator model roles and returned a hold on that put; it did not consider other stocks. The remaining $850,000 did not reach entry allocation. If several stocks are already held, smaller holdings do not receive their own wheel decisions. A large odd-lot holding can repeatedly prevent a covered call while a smaller eligible holding is ignored. One profitable put is closed per cycle even if several qualify.

Change: maintain position-level state for every underlying, process all existing obligations first, then run a distinct portfolio allocation pass for any remaining risk budget. Enforce aggregate limits centrally, including pending orders, stock holdings, and correlated exposures. Fixing this is not authorization to increase the 50% cap.

**7. P1 — There is no complete policy for losses, assignments, or portfolio drawdown. Confirmed by source.**

[The short-put manager](C:/Users/WonwooChoi/trading_bot/wheel_bot/agents/graph.py:1125) buys back winners and otherwise holds or requests manual review. It does not enforce loss, concentration, or portfolio drawdown limits. Existing covered calls lack a comparable lifecycle manager. The ordinary schedule runs twice daily. Manual review has no acknowledgment, escalation deadline, or independent alert-delivery check.

Add a risk policy for gap losses, worsening company facts, stressed assignment cash, expiry, early exercise, ex-dividend dates, and corporate actions. Poll broker activity for assignments and reconcile delivered shares and lots; Alpaca explicitly states that assignment events are not sent over the order websocket. [Options assignment documentation](https://docs.alpaca.markets/us/docs/options-trading).

Define the acceptable maximum drawdown in dollars and percent before optimizing returns. Monitor equity after external deposits/withdrawals, peak equity, portfolio delta/vega/gamma, sector/factor concentrations, and scenario losses. Model equity gaps and volatility jumps together. Position sizing should follow a loss budget, not merely use every available dollar up to a collateral cap. Stop thresholds and alerts cannot guarantee a maximum loss through overnight gaps.

Retain the prohibition on an LLM inventing liquidation authority. Establish explicit, tested human-approved emergency procedures rather than assuming that either indefinite holding or automatic liquidation is always the safe response.

**8. P2 — The candidate selector's empty result is ignored. Confirmed.**

[The screener](C:/Users/WonwooChoi/trading_bot/wheel_bot/agents/graph.py:827) filters only when `selected_tickers` is nonempty. If the selector rejects every company because of adverse news, the full original list goes to the screener, which can approve those companies again. The offline reproduction demonstrates this. The numeric fallback also disregards adverse-news exclusions, and model-returned symbols are not consistently intersected with an authorized universe.

Change: treat an empty successful selection as terminal `NO_TRADE`; distinguish an error from a deliberate rejection; intersect every output with the previous approved set. Preserve exclusion reasons. Perform the actual fundamental threshold calculations in code with quarterly dated inputs. A model can explain a business risk, but should not be trusted to enforce a three-number arithmetic filter.

**9. P1 design gap — There is no evidence yet of a repeatable investment edge.**

There is no historical options replay engine, walk-forward evaluation, calibrated outcome dataset, or return attribution system in this checkout. The “gamma penalty” is `abs_delta × 75 / sqrt(DTE)`, not measured option gamma or a calibrated expected loss. Its coefficient, the liquidity bonus, DTE band, premium band, and exit thresholds are unvalidated parameters. Bid-based yield already reflects entry spread relative to midpoint, so subtracting an additional half-spread needs a clearly defined cost interpretation.

`(1 - abs(delta)) × 100` is a delta proxy, not a measured probability of net profit. Probability of finishing out of the money, probability of eventual profit, early assignment probability, and probability of a loss-limit breach are different outcomes. Delta also changes as markets move. [OIC explanation of delta](https://www.optionseducation.org/advancedconcepts/delta).

High premium can be compensation for a known event or substantial downside, not mispricing. The five-stock universe shares substantial large-growth/technology-related exposure; ticker count is not sufficient diversification. There is no earnings blackout, ex-dividend assessment, sector cap, or correlation model. A universal debt/equity rule can also misclassify businesses with unusual equity structures; a negative denominator must not pass as attractive leverage.

Change: establish a deterministic baseline with realistic costs and dated inputs, then test whether each extra signal or agent improves out-of-sample results. Compare stock-only exposure, a cash/Treasury benchmark, and appropriate option-writing benchmarks. Evaluate alternative DTE/delta policies, early exits, and event exclusions without fitting all choices to the same history. Test a diversified ETF universe as a separate strategy variant with appropriate criteria; the existing company-FCF screener cannot simply be reused for ETFs.

**10. P2 — P&L and order-status reporting can mislead the operator. Confirmed.**

[Account reporting](C:/Users/WonwooChoi/trading_bot/wheel_bot/data_feeds.py:239) divides option P&L by `avg_entry_price × qty`, omitting the contract multiplier and using a signed short quantity. Two short contracts sold at $2 and marked at $1 have $200 profit: 50% of initial premium. The report outputs **-5000%**. Premium return is itself different from return on collateral and should be labeled accordingly.

[The transaction summary](C:/Users/WonwooChoi/trading_bot/wheel_bot/scheduler.py:159) treats `SUBMITTED` as “Transaction made.” That does not establish a fill. The strategy-input email also recomputes NLV from cash plus one stock instead of using the account NLV and all option liabilities.

Change: separate requested/submitted/filled states; report fill quantity, average price, fees, and remaining quantity. Use asset-aware signed P&L with an explicit denominator. Calculate total return from full account equity adjusted for external cash flows; attribute realized option P&L, option mark changes, stock P&L, dividends, interest, and costs. Receiving option premium raises cash and creates a liability; it is not immediate profit.

**11. P2 — The calendar and session checks are incorrect. Confirmed.**

[Holiday logic](C:/Users/WonwooChoi/trading_bot/wheel_bot/data_feeds.py:270) repeats approximate fixed dates across years. In 2026 it considers Thanksgiving, November 26, open, and November 27 closed; the latter is actually a shortened session. It also considers the observed July 3 holiday open and the ordinary September 1 session closed. [NYSE calendar](https://www.nyse.com/trade/hours-calendars).

[run_wheel](C:/Users/WonwooChoi/trading_bot/wheel_bot/scheduler.py:267) checks the date but not the current trading session. `--once` and `--run-on-start` can run outside trading hours, and the fixed 15:30 preclose schedule is unsuitable for early-close days.

Change: use the broker's current clock and authoritative exchange calendar, schedule relative to the actual session close, and enforce a final session check at execution. Use one market timezone for DTE calculations. Handle restarts and daylight saving explicitly.

**12. P2 — Test and deployment coverage needs to become representative. Confirmed.**

[Test support](C:/Users/WonwooChoi/trading_bot/wheel_bot/tests/support.py:173) replaces the real LangGraph runtime; its compiled app simply returns the initial state. Alpaca request stubs accept arbitrary keyword arguments. These tests are useful unit checks but do not validate end-to-end graph execution or broker schemas. The review's real-graph retry check did confirm three CRO rejections end in the expected abort. The installed LangChain also removes unsupported `temperature=0` for `gpt-5-mini`; I did not classify that as a current API failure.

Add integration tests with the real graph and SDK request classes and mocked network services. Cover malformed multi-leg output, duplicate and ambiguous submissions, stale data, all-position routing, partial fills, assignment, adjusted contracts, calendar boundaries, and restart recovery. Protect invariants such as no uncovered calls, no new risk after missing data, and no second submission while an intent is unresolved. Keep broker/API smoke tests separate and explicitly paper-only.

The deployment files assume a particular macOS home directory, while this checkout is on Windows. During frozen dependency installation, uv warned that the declared `wheel-bot` CLI entry point is not installed because the project is not packaged. Choose a supported production runtime, package the app, and provide a health-checked service configuration and restart runbook. This is a portability/readiness observation, not evidence that the owner's separate paper deployment is down.

**Additional hardening is worthwhile after the major defects.**

- Replace option-chain text serialization and regex parsing with typed data structures. Verify OCC symbol metadata against strike, expiry, underlying, multiplier, and deliverable. The present classifier treats anything that does not match its narrow option-symbol regex as equity, which is unsafe for adjusted contracts or unsupported assets.
- Reject NaN, infinity, negative cash/risk inputs, and fractional contract quantities explicitly. Use strict action-specific schemas with full ISO dates, explicit dollar units, bounded quantities, and forbidden unexpected fields. Use decimal monetary arithmetic at the order boundary.
- Follow contract pagination: the code requests 10,000 contracts but ignores the next-page token, and the installed SDK makes one request. Fetch held symbols directly for management even when absent from a discovery page. [Alpaca contract pagination](https://docs.alpaca.markets/us/reference/get-options-contracts).
- Add explicit call timeouts, overall run deadlines, bounded retries, model/token budgets, and health monitoring. Risk observation should remain available when an LLM or its API is unavailable.
- Separate persistent order/accounting records from LangGraph checkpoints. Checkpoints currently precede execution and do not constitute a reconciled order ledger. Close SQLite connections, define retention/backups, and make persistence failure visible instead of silently relying on memory in a production configuration.
- Rename `human_approved` or introduce an explicit authorization record: the scheduler sets it to true whenever auto-execution is enabled. That represents configured automation authorization, not per-ticket human review.
- Keep broker credentials away from research agents. The script copying keys into Cursor MCP configuration expands the places holding them; document its purpose and permissions, and avoid distributing live execution credentials to analytical components. The committed example is sanitized and local secret files are ignored.
- Limit and rotate logs, make report recipients configurable, and monitor failed notifications independently. The current loss-management policy depends on a human seeing reports.

**I would redesign responsibilities before increasing the number of language models.**

Today, the roles all use the same hardcoded `gpt-5-mini` instance and largely the same information. Giving those calls different job titles does not create independent evidence. The orchestrator's result is overwritten by deterministic routing; the screener checks arithmetic; the execution agent chooses fields that code can construct. These calls add failure paths and latency without a demonstrated benefit.

| Responsibility | Recommended implementation | Authority |
|---|---|---|
| Data quality and event monitor — add | Deterministic checks plus a model for sourced news interpretation | Block new risk on missing/uncertain facts |
| Portfolio risk controller — add first | Code evaluating every holding, pending order, and stress scenario | Mandatory approval; models cannot waive limits |
| Order and assignment reconciler — add first | Persistent service and broker event/activity processing | Manage only already-authorized intents |
| Candidate researcher — consolidate selector/screener | Dated fundamentals, news, deterministic eligibility, optional LLM explanation | Propose or veto candidates |
| Contract selection and sizing | Deterministic policy tested against history | Propose a typed ticket |
| Independent risk critic — optional | LLM searches for missing evidence and inconsistent assumptions | Veto/escalate only; cannot resize or approve around code |
| Execution broker | Deterministic adapter | Submit exactly the approved intent |
| Performance/evaluation agent — add | Quantitative reports plus an optional narrative model | Offline recommendations; no live policy self-modification |

The processing order should be: reconcile account and orders; validate data; manage every existing position; determine remaining portfolio risk budget; research eligible entries; generate deterministic tickets; apply hard risk checks; refresh/recheck; submit and reconcile. New-entry research and ongoing risk management should not share a single mutually exclusive account state.

**A selective model upgrade is reasonable after those boundaries are fixed.**

Keep `gpt-5-mini` as the evaluation baseline. Current official OpenAI guidance identifies `gpt-5.6-luna` for cost-sensitive volume, `gpt-5.6-terra` for balanced cost/quality, and `gpt-6-astra` for difficult reasoning. My proposed experiment is Luna for routine structured extraction, Terra for company/event analysis, and Astra for difficult exceptions or an independent critic. This is a workload hypothesis to test, not evidence of better investment returns. [OpenAI model catalog](https://developers.openai.com/api/docs/models).

Make model IDs, reasoning effort, timeouts, and budgets configurable per role, and record model/prompt/data/policy versions with every decision. Before switching, run the same frozen evidence packets through the baseline and challenger and score factual accuracy, source support, abstention on missing data, rule violations, prompt-injection resistance, latency, and cost. Keep a holdout set. Sample multiple runs; temperature settings do not create trading determinism. Use typed output for structure and deterministic validation for truth and authority.

For Astra specifically, current guidance requires removing unsupported sampling parameters such as `temperature`; tool calling uses Responses. The locked SDK stack predates that model, so validate compatibility rather than merely replacing a string. Account availability was not tested. [Official model guidance](https://developers.openai.com/api/docs/guides/latest-model).

**The research and acceptance plan should produce evidence before additional capital risk.**

1. **Close execution loopholes.** Disable unsupported repairs; bind orders to typed approvals; add freshness, reservations, idempotency, session checks, and the persistent order ledger. Acceptance: adverse fixtures cannot create a different trade, oversubscribe cash/shares, or duplicate an unresolved intent.
2. **Represent the whole account.** Add position-level lifecycle handling, assignment reconciliation, all-position exposure, and a stated drawdown budget. Acceptance: several underlyings, stock plus options, partial fills, and manual broker activity reconcile without orphaned positions.
3. **Replace placeholders and fix reporting.** Obtain attributable market/company/event data and correct fill and total-return accounting. Acceptance: stale or unavailable data stops new risk, every position is visible, and broker/accounting totals reconcile.
4. **Validate the deterministic strategy.** Use point-in-time options and fundamental data, delisted securities where applicable, realistic bid/ask fills, partial fills, fees, dividends, early assignment, corporate actions, cash returns, and missed sessions. Split development and forward test periods. Report CAGR, maximum drawdown, recovery time, worst month, tail loss, turnover, utilization, assignment outcomes, and costs. Include crash/volatility stress tests even if complete historical options data is unavailable. Alpaca documents options history only since February 2024; that alone cannot replay earlier crises. [Historical data availability](https://docs.alpaca.markets/us/docs/historical-option-data).
5. **Measure what agents add.** Compare rules alone, rules plus factual extraction, and rules plus analyst/critic. Reject changes that merely increase trade count, premium, or backtest fit without improving held-out net results and risk.
6. **Run an auditable paper trial.** Exercise restarts, lost acknowledgments, stale feeds, rejected orders, partial fills, expiration, assignment, and alert failures. A calendar duration alone is not a pass criterion; require actual lifecycle coverage. Paper results need execution skepticism: Alpaca says its simulator omits several live frictions and does not constrain order fills by displayed NBBO size. [Paper trading assumptions](https://docs.alpaca.markets/us/docs/paper-trading).
7. **Consider a small live pilot only after the preceding gates pass.** Size it from the agreed loss budget and liquidity, measure actual fills and slippage, and expand only with observed evidence. Do not start with the entire $1 million because the simulator accepted large orders.

**The after-tax target is a separate accounting requirement.**

Turning $300,000 before-tax profit into $200,000–$250,000 after tax implies an effective tax burden of about 33.3%–16.7%, assuming tax is the only difference. Those percentages are arithmetic, not an estimate of the owner's rate. Residence, account type, other income, loss offsets, and holding periods have not been supplied. If U.S. taxable-account rules apply, written equity options can produce short-term gains, and assignment changes stock basis or sale proceeds; covered-call and wash-sale rules can also matter. A flat “subtract 5–10 percentage points” assumption is not a tax model. Build a lot-aware export and have applicable treatment reviewed for the actual account. [IRS Publication 550](https://www.irs.gov/publications/p550).

The decisions that remain with the owner are the acceptable drawdown and recovery period, acceptable single-company and correlated exposure, whether assignment and prolonged stock ownership fit the objective, tax/account context, and the required response to urgent manual-review alerts. The first engineering priority is the execution and reconciliation boundary. The first investment-research priority is evidence of net return within an explicit loss budget.
