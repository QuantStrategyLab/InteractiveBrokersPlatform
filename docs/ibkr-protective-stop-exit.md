# IBKR protective stop exits

The native QPK `rejected:stop_loss` result blocks normal strategy allocation.
For `tqqq_growth_income` and `soxl_soxx_trend_income`, the mapper also produces
an explicit protective zero allocation for the runtime's managed securities.
The original `REJECT` and `no_execute` flags remain, and no ordinary
`risk_authority` is issued. Other rejection codes and unqualified scope remain
non-actionable.

The executor handles this allocation through a separate sell-only producer.
It sells at most the currently observed long quantity of each managed security,
using market orders. Ordinary rebalance thresholds, minimum notionals and stale
strategy target prices cannot shrink this exit. Shorts, invalid quantities,
stale stop dates and incomplete managed pending-order evidence block it.
The existing account owner fence, atomic execution claim, account journal and
final guarded submission wrapper remain required. Enabled paper admission
still requires its valid execution command and current release receipt.

`stop_event_id` binds the physical account, strategy, trading day and managed
scope. It is stable when price diagnostics change. The event uses a separate
execution timing identity, so an earlier ordinary rebalance report does not
count as a completed stop exit. No old marker is cleared. Every order intent
carries this event and its planned `remaining_quantity`; the journal binds
that metadata into the order digest. The reconciliation record carries the
event, per-security outcomes and journal attempt identifiers.

A full confirmed fill can report zero remaining quantity. Partial fills retain
the observed residual and an `UNKNOWN` final outcome. An ACK without fills,
an unknown broker status, a transport exception or a replayed claim keeps the
exact remaining quantity unknown (`null`). A conservative upper bound is
reported where an original attempt observation is available. A zero-fill
terminal rejection retains the original quantity. No error or restart creates
a replacement order or resolves a journal's `UNKNOWN` state.

This connection uses the existing native account submission contract. A
journal bound to the newer account-domain risk reservation contract remains
fail-closed if its required reservation is unavailable; this producer does not
convert a stop code into an independently verified drawdown-brake grant.
Likewise, separate strategies sharing one physical account remain subject to
the existing exclusive owner fence. A blocked second strategy is not evidence
that all positions were liquidated. No automated continuation after partial
fills or rejected orders is introduced.

Offline coverage lives in `tests/test_stop_protective_exit.py`. Its SDK-facing
venue, accounts, release receipts and storage are synthetic. These tests do
not establish production readiness, runtime state provenance, continuous stop
monitoring or real broker fills. The shared stop threshold and dependency pins
are unchanged.
