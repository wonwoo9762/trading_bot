**Implemented execution safeguards**

The owner authorized changes after the original project review. This patch changes
the trading process locally; it has not been deployed or used to submit orders.
No account settings, strategy thresholds, model selection, secrets, or dependencies
were changed. The remaining agents still use the configured `gpt-5-mini` model.

1. Execution now uses code rather than an LLM. The exact approved option contract,
   whole-contract quantity, side, opening/closing intent, and bounded cent-rounded
   midpoint produce a single-leg limit order. The broker independently rejects
   substitutions, extra legs, conflicting aliases, nonfinite numbers, and invalid prices.
2. Automated roll/spread repairs and autonomous liquidation are blocked. Distressed
   repair suggestions remain visible for manual review; direct explicitly approved
   manual liquidation through the broker remains a separate existing capability.
3. Candidate selection vetoes are enforced. Empty, missing, or failed selection
   cannot fall back to screening the whole universe. Both selection stages are
   restricted to the supplied candidates.
4. An account-scoped SQLite order journal reserves before submission. Stable client
   IDs deduplicate each action/contract per Eastern date even if price or quantity
   changes. Broker checks and unresolved reservations stop subsequent submissions.
   A submission exception triggers a lookup with the same ID, never an application
   resubmission. Any SDK-level HTTP retry retains the same client order ID.
5. Reports distinguish submission, an existing reconciled order, and an uncertain
   outcome. They no longer label order acceptance as a completed transaction.

**Operational effects**

The gate deliberately blocks all automated option orders when any account order
is open, including an unrelated manual order or a pending cancellation. This also
affects risk-reducing closes. It avoids treating pending collateral or coverage as
free until a fuller allocator is implemented. The journal persists in the ignored
`wheel_bot/data` directory and must be retained across restarts.

A timeout that never resolves at Alpaca requires manual investigation. A 404 after
reservation does not prove that a submission never happened and does not unlock
the account. Cancellation/rejection does not permit an automatic same-day retry of
that action/contract. The concurrency lock applies to workers sharing one journal;
multiple deployments and simultaneous manual activity still need coordination.

The lookup mechanism follows Alpaca's supported client-order-ID tracking API.
[Alpaca: working with orders](https://docs.alpaca.markets/us/docs/working-with-orders).

**Validation**

Final validation: **94 top-level tests passed**, including an isolated integration
runner containing **6 real-library flow scenarios**. `git diff --check` also passed.

The unit suite runs with socket connections blocked. A separate child process
loads the actual locked LangGraph, Pydantic, and Alpaca libraries instead of the
legacy unit stubs. Fixtures replace only external agent responses, data feeds,
and broker transport. Full-flow scenarios cover CSP entry, covered-call entry,
profitable put closure during a macro halt, candidate veto/failure, distressed
repair suppression, and timeout reconciliation. Unit cases additionally cover
concurrent workers, restarts, date rollover, journal failure, malformed broker
responses, pending manual orders, and changed-price/quantity retries.

These tests verify program behavior; they do not establish broker fill quality,
strategy profitability, or live readiness. No broker, LLM, or email service was called.

**Still outstanding from the review**

- Quote timestamps, freshness enforcement, session/calendar checks, and venue tick sizes.
- Live macro, company, event, and fundamental data with attributable provenance.
- Complete portfolio accounting, fresh position/collateral validation, assignment,
  expiration, corporate actions, and a defined maximum drawdown budget.
- Independent fill monitoring and accurate realized/unrealized return accounting.
- Point-in-time backtesting, stress testing, realistic costs, and forward paper evidence.

These remain higher priorities than adding another reasoning agent or assuming a
larger model will improve returns. This patch does not demonstrate a 30% annual return.
