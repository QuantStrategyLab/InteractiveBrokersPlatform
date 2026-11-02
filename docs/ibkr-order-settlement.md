# IBKR order settlement observations

The optional settlement path follows already-submitted IBKR orders for reporting.
It has no order submission, modification, cancellation or retry API. Existing
account-journal UNKNOWN fences and cash/risk reservations remain unchanged, even
when a separate reporting observation confirms a fill.

## Configuration and interface

`IBKR_ORDER_SETTLEMENT_ENABLED` defaults to `false`. Enabling it records intent
metadata before the existing granted submit and captures the broker order ID as
soon as `placeOrder` returns. After the original submission wait, a permanent ID
is captured when available. `IBKR_ORDER_SETTLEMENT_WAIT_SECONDS` defaults to `5`
when enabled and accepts finite values from `0` through `30`. Zero records the
order without an immediate follow-up wait. This does not change the QPK submit
wait or the execution report consumed by trading decisions.

The private observation namespace `ibkr_order_settlement` uses the existing
execution marker store configuration and create-only writes. Records include the
attempt, strategy, symbol, side, quantity, original client ID, submission time,
source run ID and broker order identity. Ordered observation slots serialize
concurrent writers. The reader re-reads state after each broker query, rejects
non-monotonic fills and never overwrites a terminal observation. Corruption,
incomplete scans and storage failures fail closed. The namespace is separate
from all execution-authority markers.

`POST /settle` accepts no body. It requires a verified Google OIDC ID token with
an exact `IBKR_SETTLEMENT_OIDC_AUDIENCE` and a `sub` in the comma-separated
`IBKR_SETTLEMENT_CALLER_SUBJECTS` allowlist. Missing/invalid identity returns 403,
a disabled feature or busy local cycle returns 409, and read/publish failures
return 503 with `status: UNKNOWN`. The caller cannot supply account or order IDs.
A successful response contains counts only. The route acquires the existing
process-local cycle lock; cross-process observation safety comes from atomic
marker claims, not that lock.

The broker connection is explicitly read-only with trading-permission probes
disabled. Follow-up requests have SDK request timeouts and a 15-second broker
read budget; batches over 100 unresolved orders or scans over 10,000 marker
objects fail closed. These bounds exclude connection establishment and durable
store/notification IO, which retain their existing adapter timeouts. No scheduler
is added or modified. No network call was made during offline acceptance.

## Reporting and delivery

Broker-confirmed terminal results populate `orders_filled`,
`orders_partially_filled` (a cancelled remainder with actual fills), and
`orders_rejected_or_cancelled`. Open partial fills and all unconfirmed orders
remain in `orders_pending` with reporting status `UNKNOWN`, preserving observed
quantity and price separately. No missing result becomes a rejection or zero fill.

A settlement notice reports filled quantity and price, partial n/N followed by
cancellation, or rejection/cancellation. It uses the shared masking helper for
identifiers. A durable notification attempt is reserved before sending. A
positive transport acknowledgement records a receipt; a crash or ambiguous
result remains delivery unknown and is not automatically resent.

Daily candidates amend the original source run and submission business day;
they do not add another order or trading cycle. Final fill counts require
complete original-order coverage and unique broker execution IDs. Incomplete
coverage and still-open orders remain unresolved at day end. A settlement cannot
clear unrelated risk/error incidents. Execution receipts require valid
broker-confirmed terminal quantities and prices; a label or nonterminal partial
fill is insufficient. Runtime reports include the separately observed outcomes.

## Restart and operational acceptance

After restart, the same private marker backend supplies pending identities and
prior observations. If a process dies before the broker ID is durably captured,
that intent remains UNKNOWN and is never matched by symbol alone or resubmitted.
Legacy submissions made while this feature was off are not backfilled. Late
commission evidence can remain unknown after the terminal record is frozen.

Owner approval is required on the Mac for deploying reviewed revisions, choosing
the durable store and authenticated caller, setting the new feature configuration,
and any scheduled/manual production invocation. A scheduler change requires its
own explicit approval. Paper/runtime observation and real Telegram/digest delivery
remain unproven by the fake-only tests. Live trading, Kelly and stop-loss settings
are not enabled or modified by this feature.
