# Routine notification producer observations

At each minimum-notional skip, `execute_rebalance` records the amount and
threshold already used by that branch in `notification_routine_decisions`.
The preflight no-order return and the later execution-loop skip both retain
their existing decisions, skipped-order lists, claims, broker calls and status.
The observations are private report metadata, separate from order/fill counts.

The existing unified cycle, monitor and archive bridges carry those observations
to a source-run candidate as `routine_decisions`. Several skipped symbols retain
separate comparisons. Legacy reports without the new values retain an explicit
minimum-notional reason with unknown amounts; amounts are never reconstructed
from account equity, holdings or another run.

`DIGEST_ACCOUNT_ALIAS` is an optional ordinary display key supplied by the owner
in the existing target-bound notification configuration. Its value must be the
same canonical alias used by the website account directory. A unique matching
`DIGEST_TARGET_BINDINGS_JSON` entry can supply it. An ambiguous or mismatched
target loses this display key along with the existing protected identity
fallbacks. The producer never derives it from broker account IDs or target IDs.
The central resolver displays it only after a safe same-platform name lookup.

Conflicting display observations for one event/run are withheld, while execution
counts, event kinds, receipt validation, incident state and routing retain their
existing meaning. The alias is private candidate metadata, excluded from public
receipt summaries and public notification fallbacks.

This increment is based on the reviewed notification-only source `07a98e1`.
The original frozen release ref, build input, three routing values, dependency
pins, production revision and scheduler remain governed by the existing rollout.
Review this producer increment before preparing any later image. Routine sender
retirement still requires same-window producer coverage and real central
delivery; real order/fill events and urgent incidents retain immediate routing.
