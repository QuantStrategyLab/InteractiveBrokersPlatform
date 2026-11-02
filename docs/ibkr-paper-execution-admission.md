# IBKR PAPER execution admission

## Cash-only equity containment

The ordinary rebalance executor blocks cash-only equity buys with
`SETTLED_CASH_LEDGER_UNVERIFIED`, independently of the opt-in PAPER guard
below. This applies to paper and live execution and dry-run equity previews.
The current native path has no verified, timestamped settled-cash ledger that
accounts for shared reservations, open-order obligations and actual commissions.
Neither a positive `CashBalance` or `SettledCash` tag nor projected sale proceeds
authorizes a new equity buy. Otherwise-qualified position reductions retain
their existing claim, account journal and unknown-outcome checks. A buy-only
cycle blocked here does not take the daily execution lock.

This is containment, not a working settled-cash allocator. It blocks positive
cash-only equity allocations too. Margin-mode behavior and the separate option
intent path are outside this containment; neither is certified by it. No new
configuration switch or production setting is introduced. Do not change an
account to margin mode to bypass a cash-only block. Cash lifecycle, commission
reconciliation and cross-cycle cash allocation remain unproved.

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
