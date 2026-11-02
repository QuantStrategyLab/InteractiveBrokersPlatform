# IBKR PAPER execution admission

## Cash-only equity settlement gate

Cash-only equity buys use explicit finite `SettledCash` observations in the market
currency for every selected account. The reader uses the blocking ib_insync
`reqAccountUpdates(account)` API and captures matching `accountValueEvent` rows
received during that refresh. It requires completion (`accountDownloadEnd`), a
10-second request timeout (or the existing shorter timeout) and observations no
older than 30 seconds. Timeout and request-error settings are restored afterward.
Cached rows, missing refresh support, completion without a matching settlement
update, stale timestamps and incomplete refreshes cannot authorize buys.
Missing, anonymous, unreadable, malformed,
non-finite or conflicting settlement rows block new buys with
`SETTLED_CASH_LEDGER_UNVERIFIED` before taking a buy-only daily execution lock.
Zero and negative settled cash provide no buy budget. `CashBalance`,
`AvailableFunds` and fallback buying power cannot establish settlement.

Positive settled cash goes through the existing reserve subtraction, buffered
buy sizing, quantity rules, account journal and pending-order checks. Symbols
share the remaining budget within a cycle. Settlement is refreshed before the
buy phase even when no sale occurred, and again before sizing each buy. A lower
valid balance reduces the remaining budget; zero, negative or unknown settlement
blocks the buy. General cash tags cannot override that result. Observations that
expire during sizing block submission. The same deadline is checked again
immediately before `placeOrder`, after claim acquisition, journal reservation
and contract qualification. Expiry at that boundary raises without calling
the broker; the existing journal retains the consumed attempt as `UNKNOWN`
and requires reconciliation rather than retrying or assuming rejection.
After a sale, only a completed fresh
settled-cash observation supplies spending authority; projected sale proceeds
remain telemetry. Dry-run previews do not credit sales as settled cash.
Otherwise-qualified reductions retain their existing protections. The gate does
not change target weights or generate protective-exit targets.

These freshness and submission rules are verified with synthetic refreshes and
offline SDK callbacks. Runtime broker-refresh reliability, cross-cycle cash
conservation, commission reconciliation and full acceptance remain unproven.
`OBSERVED` describes this completed-refresh evidence, not an authenticated cash ledger.
The existing account journal and incomplete pending-order blocks are preserved;
there is no new allocation engine or durable principal-reservation ledger.
Margin mode and separate option intents keep their existing behavior and remain
outside this equity settlement claim. No production switch is changed.

## Opt-in PAPER command guard

`IBKR_PAPER_EXECUTION_ADMISSION_ENABLED` is an opt-in guard for the ordinary
IBKR `execution_mode=paper` rebalance path. Its default is disabled. It
requires the shared runtime target to explicitly declare
`execution_environment=paper` with `dry_run_only=false`. It does not apply to
`IBKR_PAPER_LIQUIDATE_ONLY`, local dry-run previews, live execution,
deployment workflows, or schedulers.

When explicitly enabled, the strategy/control-plane producer must place a QPK
`execution_command.v1` object at `signal_metadata.paper_execution_command`.
The command must be content-addressed, use `execution_mode=paper`, and contain:

- the exact promoted `strategy_release` identity;
- an embedded `paper_risk_admission_receipt.v1`; and
- the same effective session, profile, and account scope as the paper cycle.

Before any order adapter is invoked, the platform verifies that command and
receipt, self-attests the runtime release, and derives every exposure effect
from the current position quantities and quotes used by the cycle. Invalid or
missing evidence, an unmodelled option intent, or a `reducing_only` receipt
that would increase exposure blocks the whole cycle. The safe receipt,
reconciled exposure facts, and enforced runtime-gate receipts are persisted in
the normal reconciliation record under `paper_execution_admission`.

Do not enable the flag until an upstream producer can supply this immutable
command contract. Enabling it for a non-PAPER, local dry-run, or ambiguous
runtime target fails at startup rather than silently weakening the guard.
