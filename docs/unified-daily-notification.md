# Unified daily notification (code only)

When no target weights are produced, the heartbeat reports that the strategy
rebalance decision is unavailable. Snapshot failure details remain visible; an
absent decision does not establish that no rebalance is needed, and independent
account protection operations may still occur.

`QSL_ROUTINE_NOTIFICATION_MODE` defaults to `immediate`. Exact `daily_digest`
opts into one central bilingual digest. `QSL_DIGEST_ROUTINE_ORDERS=false` retains
immediate confirmed order notices. Failed, pending, uncertain, risk, persistence
and authentication outcomes retain immediate alerts. Healthy idle cycles and
routine plugin observations do not produce individual pushes.

Shared notification modules are imported directly from the pinned QPK package
(`db70e1ca647ee29577432233d9dc2ea9e6eb1c4e`): `display_names`,
`digest_candidates`, `digest_routing` and `unified_contract`. The local
`notifications/digest_snapshot/digest_archive.py` remains an IBKR-specific
vendored copy because it adds IBKR archive persistence hooks. Existing
QPK/strategy pins, order execution, funding, risk, account enablement and
durable report writes are unchanged.

Before enabling routing, validate the exact active target's private archived
report, candidate store and QRS receiver. Existing cloud monitor wiring uses
`RUNTIME_TARGET_ENABLED=true`, exact protected `RUNTIME_TARGET_JSON`,
`EXECUTION_REPORT_GCS_URI`, `DIGEST_CANDIDATES_GCS_URI`, `DIGEST_STORE_TARGET`,
`DIGEST_OPAQUE_ACCOUNT_UID` and `DIGEST_TARGET_ID`. Missing configuration stops
projection; disabled targets stay disabled. Do not infer private identities.
The producer aggregates every unique cycle in the completed 06:00 SGT window;
missing evidence is unknown, never zero. No new collector or scheduler is added.

An explicit `risk_gate=APPROVE` with only the `risk_gate:passed` marker is
normal approval evidence. Notification classification uses a copy without that
marker; the execution result and risk decision remain intact. Mixed, unknown or
restrictive flags, pending orders and errors retain immediate delivery. Server
logs record only the resulting notification kind, without private event facts.

Before uploading a legacy `primary-live` projection, the existing scheduled
producer reads the exact configured service's serving revision metadata. Once
that revision enables unified routing with the same slot, candidate namespace
and complete notification identity, the legacy projection stays local so it
cannot overwrite the runtime's cumulative candidate. Unknown writer state
stops that upload. Other target slots keep their existing path. This check
reads cloud metadata only and never invokes a strategy or broker endpoint.

No deployment, variable application, IAM, secret or live message is authorized
by this PR. Source adoption and delivery require separate approved release work.

For multiple active targets, protected `DIGEST_TARGET_BINDINGS_JSON` supplies a
list of per-target environment mappings. `DIGEST_NOTIFICATION_SCHEDULE_JSON`
can reuse the existing heartbeat scheduler/calendar declaration to prove a
window was not due. Missing due reports stay errors; closed configured sessions
produce an `actually_ran=false` marker without inventing zero trading counts.

Fill counts require individual execution identifiers. A completed order response
proves an order completed, not how many fills it received. Missing fill counts
remain unknown; confirmed routine orders still appear without a false diagnostic.

Runtime Guard notifications keep the localized summary and workflow link. Raw
provider audit entries remain in their original restricted logs and are not
passed as Telegram technical details, including when the deployed formatter is
pinned to an older shared package. Guard detection, deduplication and failure
exit codes are unchanged.
