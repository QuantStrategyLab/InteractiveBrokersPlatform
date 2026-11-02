# IBKR PAPER execution admission

## Cash-only equity settlement gate

Cash-only equity buys use explicit finite `SettledCash` observations in the market
currency for every selected account. Missing, anonymous, unreadable, malformed,
non-finite or conflicting settlement rows block new buys with
`SETTLED_CASH_LEDGER_UNVERIFIED` before taking a buy-only daily execution lock.
Zero and negative settled cash provide no buy budget. `CashBalance`,
`AvailableFunds` and fallback buying power cannot establish settlement.

Positive settled cash goes through the existing reserve subtraction, buffered
buy sizing, quantity rules, account journal and pending-order checks. Symbols
share the remaining budget within a cycle. After a sale, only a new explicit
settled-cash observation supplies spending authority; projected sale proceeds
remain telemetry. Dry-run previews do not credit sales as settled cash.
Otherwise-qualified reductions retain their existing protections. The gate does
not change target weights or generate protective-exit targets.

These are offline component guarantees, not proof of broker-cache freshness,
cross-cycle cash conservation, commission reconciliation or runtime acceptance.
`OBSERVED` means a settlement tag was read, not an authenticated cash ledger.
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
