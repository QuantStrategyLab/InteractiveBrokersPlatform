# Unified daily notification (code only)

For compatibility, a legacy heartbeat with no decision or error fields retains
its no-rebalance wording; this absence is not evidence of verified health.
When a snapshot fails closed, a decision is explicitly unknown, or the decision
has an error, the heartbeat instead says the strategy rebalance decision is
unavailable, preserves failure details and remains eligible for an alert even
when routine no-trade notifications are disabled. Independent account protection
operations may still occur.

If an independent account brake has a submission status but its order details
are absent from the strategy order list, the runtime report marks the submitted
order count unknown and identifies that coverage gap. The reconciliation list
remains the strategy-order list and carries the same coverage marker. A status
alone does not establish a fill. This does not supply the missing order receipt;
downstream readers must honor the coverage marker, and their complete coverage
has not been verified.

Routine cycles, broker-identified orders and healthy heartbeat observations
are persisted for the central bilingual daily digest regardless of legacy
routing flags. An acknowledgement or partial fill does not establish a full
fill. Missing or conflicting broker identity, unknown outcomes, reconciliation
failures, risk, persistence and authentication errors retain immediate alerts.
If a candidate cannot be persisted, the runtime sends an operational failure
notice rather than the routine message and does not report the cycle deferred.

The runtime guard uses a generic service label in operator messages and omits
project identifiers. A failure-log count is an observation: its cause and the
current state require verification. It does not establish a failed trade or a
recovery. The workflow link remains available for investigation. Guard alert
criteria, target selection and deduplication are unchanged.

Shared notification modules are imported directly from the pinned QPK package
(`dcbd9c601fc8f1ddb9be9679a38e9f748dd03da6`): `display_names`,
`digest_candidates`, `digest_routing` and `unified_contract`. The local
`notifications/digest_snapshot/digest_archive.py` remains an IBKR-specific
vendored copy because it adds IBKR archive persistence hooks. Strategy pins,
order execution, funding, risk, account enablement and
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
restrictive flags and errors retain immediate delivery. Server
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

Runtime Guard notifications keep the localized summary and clickable workflow
link while disabling Telegram web page previews. Raw
provider audit entries remain in their original restricted logs and are not
passed as Telegram technical details, including when the deployed formatter is
pinned to an older shared package. Guard detection, deduplication and failure
exit codes are unchanged.



## Publication before the central daily read

GitHub schedule events can start late. A heartbeat workflow is therefore not a
reliable deadline trigger for the 07:00 Asia/Shanghai read. Keep its existing
monitoring checks, but run the archive-only publisher independently in an
approved Cloud Run Job using the platform image and its frozen dependencies.

Command: `/app/.venv/bin/python -m scripts.publish_digest_before_read`.
Use `--help` for an offline CLI check; it exits before publication or the watchdog.
The image keeps uv-managed Python under `/opt/python` so `appuser` can traverse
its path. Build checks and runtime commands use `/app/.venv/bin/python` directly;
the Docker build also checks publisher help after switching to `appuser`.
The command imports the existing producer, never the service entrypoint. It does
not run a drill, refresh a broker session, invoke a trading route, send Telegram,
or access Secret Manager. Archive reads use the Storage SDK and the job's
attached identity; the image does not need gcloud. Existing candidate writes,
merge semantics and identity checks remain authoritative.

Use one job per existing central target, with its exact protected runtime JSON,
archive prefix, opaque UID, target ID, destination and slot. No identifiers can
be inferred from a display label. Supply the existing notification schedule so
an idle candidate is only produced when the completed window was not due.
Multi-target binding JSON is rejected by this entrypoint: one successful target
cannot mask another missing target.

The job requires `DIGEST_PUBLISH_ENABLED=true`,
`QSL_ROUTINE_NOTIFICATION_MODE=daily_digest`, and the existing
`RUNTIME_TARGET_ENABLED=true`. These are publisher-job settings, not changes to
the trading service. Preserve the platform's existing unified-notification flag
and any notification marker prefix needed by its existing producer. Do not
inject broker credentials or Telegram token secrets into this job.

The deployment proposal is:

| Setting | Proposed value |
| --- | --- |
| Cloud Scheduler | `10 6 * * *`, timezone `Asia/Shanghai` |
| Scheduler target | OAuth-authenticated Cloud Run Jobs `:run` API for this job |
| Scheduler retries | 0; do not blindly retry an uncertain execution request |
| Job tasks / parallelism | 1 / 1 |
| Job retries | 0; an uncertain write is not proof of failure |
| Job task timeout | 600 seconds |
| Image | Reviewed immutable platform image containing this entrypoint |
| Publication window | Start at or after 06:00 and before 06:40 |
| In-process watchdog | 600 seconds, including projection and readback |
| Readback deadline | Before 06:55, leaving five minutes before the reader |

The business day is the day before the job's Shanghai date, for the completed
[06:00, 06:00) window. A normal run starts at 06:10 and finishes by 06:20. A late
start, disabled/missing binding, due-but-absent report, unconfirmed write, wrong
business day or identity, failed readback, or expired deadline exits nonzero.
An interrupted write remains unknown and must be inspected before any retry.
The success record contains only the business day and a verified-readback flag.

Deployment, Scheduler creation and IAM are separate operations requiring their
own authorization. The Scheduler identity needs permission to execute only the
job; the job identity needs the existing archive-read and candidate-store
permissions (including notification state used by the existing producer).
Do not substitute a project-wide role or copy the trading runtime's credentials.
A merged change or a successful unit test does not prove the job is deployed.

After a separately authorized rollout, verify a natural job execution and the
exact candidate object generation created before 07:00, then check the central
reader's date, slot, identity and coverage. A Scheduler API acceptance alone is
not publication success. Keep missing/invalid coverage visible; do not substitute
old candidates, zeros, or an artificial `not_due` result.

References: [GitHub schedule timing](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
and [Cloud Run scheduled jobs](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule).
