# IBKR account facts report projection

`project_ibkr_account_facts_history()` is a pure projection helper. Its caller supplies an archived `runtime_report.v1` and independently established deployment metadata. The explicitly enabled workflow CLI reads that report from the fixed GCS prefix and can send one validated projection to QRS. It does not access Cloud Run, Secret Manager, or an IBKR Gateway.

The helper accepts only an IBKR Cloud Run report whose `project_id`, `service_name`, `account_scope`, `runtime_target.account_selector`, `runtime_target.deployment_selector`, and `diagnostics.runtime_revision` match the caller's expected values. A one-item, non-default account selector must match the single physical `summary.account_facts.account_ids` value. If `runtime_release_receipt.runtime_revision` is present, it must match `diagnostics.runtime_revision`; legacy unattested receipts need not carry that field and are preserved unchanged.

The projected record uses `schema_version: "ibkr_account_snapshot_history.v1"` and `snapshot_schema_version: "ibkr_account_snapshot.v1"`. It carries the native `account_ids`, the actual aware `summary.account_facts.observed_at` as a point observation interval, `broker_reported_balances: [{currency: "USD", net_assets: string|null}]`, and native cash rows shaped as `{currency, cash_balance, source_tag}`. The account observation must fall inside the enclosing runtime report interval; publisher freshness is checked against this account observation, not the report wrapper. Cash values remain decimal strings, including zero, negative, and small values, within 15 total digits and 8 fractional digits; `source_tag` must be one of the five native IBKR cash tags already selected by the producer. `net_assets: null` means the source did not provide a trusted NLV; the schema still identifies that balance as USD. No `available_cash` or strategy adjusted cash is synthesized.

`source_binding.id` is SHA-256 over sorted, compact canonical JSON containing only `project_id`, `service_name`, `runtime_revision`, `account_scope`, the original one-element `account_selector`, and `deployment_selector`. It excludes amounts, report URI, and token versions. The report URI is used only for GCS prefix validation and is never added to the history body. Native account IDs must match `U` or `DU` followed by digits.

The workflow publisher is disabled by default. A manual run must select the generic `primary-live` action; the existing daily heartbeat schedule invokes the same publisher in a separate job so its outcome is visible even if the heartbeat check fails. It lists only the report prefix supplied by protected configuration, selects the newest timestamp-named report across the current and preceding month, and never falls back to an older report if the newest report is stale or lacks account facts. Freshness is based on the actual aware account observation within the existing 36-hour display window (and at most five minutes in the future); the timestamp is not rewritten. The publisher does not run a broker query, Cloud Scheduler job, or Cloud Run request.

All private target mapping is supplied by protected Secrets rather than public workflow values: `IBKR_ACCOUNT_FACTS_REPORT_PREFIX`, `IBKR_ACCOUNT_FACTS_TARGET_ID`, `IBKR_ACCOUNT_FACTS_PROJECT_ID`, `IBKR_ACCOUNT_FACTS_SERVICE_NAME`, `IBKR_ACCOUNT_FACTS_RUNTIME_REVISION`, `IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE`, `IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON`, and `IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR`. `IBKR_ACCOUNT_FACTS_SYNC_TOKEN` remains the dedicated publisher credential; `IBKR_ACCOUNT_FACTS_SYNC_URL` remains an exact-match protected variable. Missing or malformed mapping, an absent account-facts observation, stale observation, or any metadata mismatch stops without POST.

The existing publisher job explicitly enables optional positions for `primary-live`
and `additional-1` through `additional-3`. It forwards only `stocks_only` rows
already accepted by the same report projector, including optional native average
cost; it makes no extra broker read. This scope does not establish a complete
account or all-asset inventory. Missing or invalid positions remain omitted, and
an empty list requires the existing explicit completeness evidence rather than
being inferred from missing fields. All source, identity and freshness guards
still apply independently of this transport opt-in.

Publishing requires the exact configured sync endpoint and dedicated `secrets.IBKR_ACCOUNT_FACTS_SYNC_TOKEN`. It makes one HTTPS POST attempt without redirects or retries, and prints fixed status/reason codes only. The publisher reports only the transport/storage result; daily automatic refresh is accepted only after a later natural runtime report carries a newer original `account_facts.observed_at`. Replaying an unchanged report is not evidence of an automatic refresh. This publisher does not use the legacy execution-evidence token.

The existing read-only `diagnose_account_facts_identity.py` also reports `account_facts_stages` from the same in-memory report. Its original projection, mismatch fields, protected revision expectation and CLI exit gate remain authoritative and unchanged. A revision mismatch still rejects the report. The additional stages do not substitute a report revision into that expectation, construct a publishable history record, make another cloud read, or publish anything.

Each stage is only `verified`, `invalid` or `unknown`: report envelope, report-prefix provenance, exact non-revision scope identity plus optional receipt/report revision consistency, snapshot schema/currency, physical account selector, observation interval, native decimal/cash structure, and optional stock-position structure. These predicates reuse the publisher's existing parsers. A provenance, envelope or scope failure leaves dependent snapshot stages unknown; an account or interval failure leaves dependent amount/position stages unknown. Missing/unsupported optional positions remain unknown; explicit `stocks_only` facts that cannot pass the existing position projector are invalid for that diagnostic stage and remain omitted by the unchanged publisher. Unexpected optional-probe errors stay unknown without replacing the original guard result.

For a fresh report with verified envelope, provenance, scope, snapshot, account and observation interval, if its revision equals the ready revision and is the sole 100% serving revision, the diagnostic adds `serving_revision_evidence` with `status=observed` and `serving_revision_sha256`. The digest is SHA-256 of the UTF-8 revision name, matching the existing additional-account recovery input. Otherwise the evidence is absent. It is an observation for review, not release approval or permission to publish or rotate a source. The recovery workflow independently rechecks the report, identity and serving revision; the diagnostic does not disclose the revision name or change the original publisher guard.

`verified` means only that the stated structural/scope predicate passed. It does not establish account ownership, financial accuracy, source approval, a full identity-guard pass, release/owner approval, loaded runtime attestation, receiver acceptance or natural producer recovery. Legacy optional receipts keep their original treatment and do not gain an approval state. Public output includes no amounts, position rows/counts, observation timestamps, private identities, report locations or exception details.

`snapshot_issue` classifies the same existing snapshot-schema stage using only fixed codes. Before scope proof, preflight or an unavailable report, it is `unknown`; a verified schema/currency stage yields `none`. An invalid stage is classified in predicate order as `summary_absent`, `summary_not_object`, `account_facts_absent`, `account_facts_not_object`, `snapshot_schema_missing`, `snapshot_schema_invalid`, `currency_missing` or `currency_invalid`. Explicit null or a wrong type is invalid/not-object rather than an absent field; unexpected classification errors remain unknown. The classifier reads the same report in memory and leaves all eight stage meanings and the original rejection/CLI exit unchanged.

`none` proves only the existing snapshot schema and USD label predicates, even if account, observation, balances or positions later fail. A fixed issue identifies the observed shape, not why the producer omitted or malformed it. Current source can omit facts before portfolio work or when native facts/account-selector checks fail; the diagnostic does not choose among those causes, copy report status/error text, synthesize missing facts, convert currencies, select an older report or claim the serving source is approved.

The explicit paper-liquidation rehearsal also carries optional account facts from the same native snapshot already read by `get_current_portfolio`. The existing projector supplies those facts before the snapshot is reduced to its legacy positions/equity/buying-power view; missing, invalid or failed optional projection keeps that legacy view unchanged. A module-private dictionary marker limits this carry to the adapter's projection; it holds only existing projected fields, never the raw native object, and the cycle emits an ordinary dictionary. A plain legacy sidecar remains unknown even if it claims the snapshot schema or copies its shape. The marker creates no source, ownership or release approval. The cycle uses only the last existing read, including the original single empty-position retry, and preserves the native observation timestamp. These are pre-liquidation facts, not post-fill balances or evidence of a refreshed account. No cash, account ID or observation time is synthesized. The original report selector and publisher guards still apply. There is no extra broker read, new reporting channel, execution-policy change, order/notification change, source-identity approval or production-restoration claim; the isolated paper command consumer remains separate.

`report_stop_reason` exposes only an allowlisted category already declared by the same report, after existing envelope, provenance and scope checks pass. A skipped report may declare `already_running`, `market_closed` or `ibkr_fractional_equity_api_unsupported`; an error may declare `strategy_execution_blocked`, `strategy_deadline`, `ibkr_gateway_unavailable`, `ibkr_trading_permission` or `ibkr_portfolio_snapshot`. Unknown categories become fixed `unclassified_skip`/`unclassified_error`; contradictory status/reason fields become `invalid_stop_context`. Preflight, failed reads, unproven scope, pending/unrecognized status and unexpected optional errors remain `unknown`. `no_declared_stop` means only an `ok` report has neither stop field.

The category is a report declaration, not independent proof of the running process, the reason account facts were omitted, gateway credentials, account ownership or financial accuracy. It exposes no free text, error bodies, counts or private values. The same report is reused without additional cloud or broker reads; original stages, projection guard and CLI exit remain unchanged.

The `--broker-contract-source-evidence` mode also classifies the producer's
top-level `market_exchange` as `SMART_REPORTED`, `OTHER_REPORTED`,
`NOT_REPORTED`, `NULL` or `INVALID`, after the original identity and freshness
checks. It exposes no raw exchange value and does not fill missing routing
from source-code defaults or nested guesses. This is the report's routing
configuration, not an executed order route, an account fee-plan attestation or
a complete fee estimate. A Tiered pricing reference and SMART configuration
must still be distinguished from actual commissions and other charges.

# Archived account-facts inspection

`inspect-account-facts-archive.yml` is a manual read-only diagnostic for one
existing additional-account slot and one past UTC date within the receiver's
retention window. It lists at most 64 exact report names below the protected
prefix and applies the original pure account-facts projector without replacing
the revision, account identity or observation time. Its output contains only
names, dates, fixed skip reasons, availability flags and opaque hashes. An empty
listing remains distinct from a read failure. No ingress token, publisher,
notification, broker request, source adoption or business write is involved.
An inspected archive is not evidence that its daily row was published.
The optional `scope=month-metadata` mode lists up to 2,048 report filenames in the
selected month and reports counts by filename date, without reading financial
bodies. Filename dates establish archive availability only, not observation
dates or financial validity. It does not silently truncate an oversized listing.
When only the configured runtime revision differs, the reader may emit a
`historical_candidate` hash after validating all remaining original report,
account, receipt, snapshot and date predicates against the declared revision.
The original `skipped` result remains unchanged and receiver admission is
explicitly unverified. Compare the hash with the protected receiver's admitted
historical sources separately; this preview never authorizes a source rotation
or publishes the report.
The explicit `scope=month-snapshots` mode checks at most 64 original reports
from that month in the protected runner; an oversized month stops before any
body read. Each report keeps its own original date predicates. The default
`scope=day` remains a single-day inspection. All scopes remain read-only.
Unexpected failures expose only an allowlisted error class and a validated
timestamp filename; exception messages and stack traces remain suppressed.

# Reviewed archive recovery

`publish-account-facts-archive.yml` is a manual main-only, first-attempt tool.
The default `preview` accepts at most 31 timestamp filenames with independently
reviewed source and projection hashes. Every original report is reprojected
against the same protected project/service/account/deployment tuple, its own
declared revision and observation date. The original deployed revision must
still be readable and have an immutable image and the exact matching runtime
target. Missing provenance or any changed projection stops before writes.

`seal-source` produces only a GitHub-public-key-encrypted request for the
existing receiver `IBKR_ACCOUNT_FACTS_SOURCE_ROTATION_JSON` contract. Its current
source stays unchanged and only the selected same-account historical IDs are
requested. The existing receiver CAS workflow and a separate readback must
admit the request; sealing does not change a binding.

`publish` uses the existing platform ingress token only on the explicit
`/api/account-facts/history/sync` endpoint. All records qualify before the first
POST. Each day is sent once, in date order, and the exact historical/latest-
unchanged/account/target/date/time ACK is required before continuing. A conflict
or unknown outcome stops without retry. The receiver alone admits sources and
inserts missing days; existing rows, latest, positions, returns, executions and
external flows are never replaced. Original decimal values and timestamps stay
in the protected runner. No broker, deployment, notification, IAM or automatic
schedule is involved, and the ordinary 36-hour freshness window is unchanged.

This workflow's manifest contains only filenames and opaque hashes. Only the
encrypted source request is retained as a one-day artifact; financial payloads
and credentials are not logged or uploaded. Successful ACKs still need the
authenticated read-model and latest-isolation acceptance check.

# Zero-traffic maintenance cash observation

The one-shot maintenance Job now sends one `reqAccountSummary` with group `All`
and tags `$LEDGER:USD`, the documented [per-currency cash-tag selector](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-summary/account-summary-tags).
It replaces the previous three-tag request; there is no fallback or second
request. The producer records `request_tags` and retains only supported native
cash fields for the selected account in USD. BASE and other currencies are
counted for diagnosis without storing their values or treating them as USD.
Unknown tags are not aliased to SettledCash. Missing fields remain missing;
equal decimal repeats may share one observation, while conflicting repeats
remain invalid. The selector does not guarantee that SettledCash is returned.

Readback verifies the explicit ledger selector, the complete fixed diagnostic
tag set and USD-only values before emitting value-free states. Archived
three-tag reports without `request_tags` keep their existing validation and
are not rewritten or upgraded to ledger observations. Neither path establishes
complete obligations or spending authority. Offline tests prepare this query;
merging the change does not execute a Job, change Gateway settings or enable a
configuration. A new live read still needs its applicable execution authority.

The one-shot maintenance Job's explicit summary read must observe the matching
`accountSummaryEnd` callback and propagate request errors. The pinned SDK can
otherwise resolve its request future successfully on an error. During this
single read, `RaiseRequestErrors` is enabled and then restored, together with
both original callbacks. Later subscription updates are excluded from the
initial observation. No additional request or retry is introduced.

The manual `completed_summary_logs` mode instead reads only an already completed
execution's existing logs and digest-verified private report. It is exclusive
with candidate and Job modes and uses the same protected account scope. Only
fixed status, counts, numeric SDK error codes and opaque source hashes leave
the runner. Its bounded historical interval is not current cash evidence;
an absent parsed error does not prove the request succeeded, and interval
coincidence does not establish the summary request ID. It never creates or
executes a Job, connects to a broker, changes settings or republishes funds.

The existing `primary-account-facts-readonly.yml` manual workflow keeps its
original report-only behavior when `maintenance_source_commit` is empty. An
explicit reviewed source SHA selects one current ready, zero-traffic candidate
with the same complete account/runtime configuration as the configured serving
revision. The maintenance application must include the read-only Gateway guard:
nonzero configured client identity and one connection attempt, because locked
ib_insync can bind manual orders for client ID zero even with `readonly=True`.

Candidate mode reuses the existing authenticated `/probe`, warm-up caller and
private report storage. A temporary traffic tag and Scheduler job make the
internal request without shifting normal traffic. It verifies the scheduled
target, disables Scheduler retries, removes only this run's temporary resources,
and reads back the original baseline. Failed or ambiguous writes are not retried.
Raw reports and projections stay in the protected runner; the artifact contains
fixed states, source/hash/time and no amounts or account identifiers.
The tag uses as much of the base36 run ID as fits Cloud Run's combined
46-character tag/service limit. The complete ID remains in the Scheduler name.
Existing tags are never replaced; an occupied name or unsupported service
length stops before mutation.

The fixed maintenance request header selects the original writer's
`maintenance-cash/` child prefix. Both success and failure reports are isolated
from the live publisher's monthly listing; the reader requires this namespace
and the marker. Use a reviewed maintenance application that includes this
isolation, in addition to the read-only client guard. No publisher binding or
live report is rewritten or deleted.

The output is explicitly candidate-only. It does not update the publisher's
revision binding, activate the runtime, run a strategy, establish product/API
permissions, verify complete pending cash obligations or authorize spending.
It cannot count as the full no-submit trading cycle.

# Serving deployment label observations

The existing `--serving-source-metadata-only` diagnostic exposes optional
`reported_source_sha` and `reported_deployment_run_id` from the selected serving
revision's `commit-sha` and `github-run-id` labels. Each value passes the original
SHA or positive-decimal run-ID format check; malformed labels remain null and
are not echoed. These public locators remain available when a later deployment
receipt check fails, so the operator can inspect the exact GitHub run and job.

Reported labels do not establish deployment provenance, loaded source or release
approval. `selected_source_sha` is still set only after the original repository,
workflow, job, steps, selected source, immutable digest, scope and stable-serving
checks pass. Failed checks keep their original reason and unknown verification
states. This mode still performs no financial report read, broker connection,
publishing or configuration write.

### One ordinary additional-1 probe

`additional1-account-facts-probe.yml` is manual, main-only, first-attempt-only,
fixed to the existing `additional-1` slot. It uses the existing recovery WIF
provider/deployment identity and changes no IAM, service, Scheduler, account,
strategy, risk or notification configuration. The source allowlist contains only
`8bcd327327acc3e47a88a6a0901bf91b06ef30c7`, whose ordinary `/probe`, synchronous
strategy-deadline cleanup and single-worker image command were reviewed.
Its change from the previous reviewed source only adds fixed classification of
timed-out contract-qualification callers; the ordinary probe path is unchanged.
A new serving source still requires fresh complete static/source verification
and a new reviewed preflight fingerprint object; previous results cannot be reused.
A changed source requires review of the actual path before updating that list.

1. Choose `source_evidence=deploy_receipt` (the default) or explicitly choose
   `static_source`, then dispatch `mode=preflight`, leaving
   `reviewed_fingerprints={}`. No service HTTP
   or broker request is sent. The protected runner checks the selected source evidence, non-revision
   account scope, canonical Cloud Run URL, settled
   unique 100% serving revision, revision and service-template concurrency/caps
   of one, and no image-command override (or the exact reviewed one-worker,
   one-thread command). The existing native group reader checks the configured
   positive client ID, Gateway backend and physical account selector. It reads
   only the declared private group configuration using existing permissions.
2. Review all safe `checks`, the six `fingerprints`, and the bridge job,
   OIDC identity and configuration hashes in `additional1-probe.safe.json`.
   Preflight uniquely matches the existing Scheduler to the verified canonical
   service URL plus `/probe`: enabled, POST, audience equal to the canonical URL,
   Google service-account OIDC identity, an empty body or exact empty JSON object
   `{}` (base64-encoded by Scheduler), and no custom headers (the standard
   `Content-Type: application/json` or `application/octet-stream` and default
   Scheduler User-Agent are allowed unchanged), 180-second
   deadline, zero retry count and zero maximum retry duration. Its complete
   selected configuration enters the existing `state_sha256`; no private job or
   identity mapping is committed. The source check stays between two service and
   bridge readbacks. A new bridge attempt during that check invalidates it.
   Coordinate writers and calls to this service during the operation, then
   dispatch explicit `mode=invoke` with exactly the reviewed six-fingerprint
   object and the same `source_evidence`. Invoke repeats the complete preflight;
   changes stop before obtaining the existing WIF identity's Google access token.
3. Invocation uses only that existing Scheduler. The caller rejects a recent
   same-job attempt (within the 180-second deadline plus 15-second observation
   allowance) or an upcoming natural attempt in that interval. It also requires
   a unique Started/Finished pair for the exact previous `lastAttemptTime`, with
   unchanged current job metadata: elapsed age alone never proves completion.
   It journals the
   uncertain outcome before **one** Google Scheduler `jobs.run` POST, with no
   redirects, proxies or retries. It never directly calls the Cloud Run URL or
   prepares a native ID token, and never creates or modifies a Scheduler job,
   ingress or IAM. Missing permissions stop without a fallback. Timeout, rejected
   dispatch, missing/ambiguous terminal metadata or workflow interruption require
   inspecting the original attempt before any later action; an existing dispatch
   journal prevents a second invocation in that runner.
4. Readback stops within a 195-second monotonic polling budget; each metadata
   command timeout is limited by the remaining budget. It uses only fixed
   Scheduler control metadata: one `AttemptStarted`/`AttemptFinished` pair, a
   corresponding new `lastAttemptTime`, unchanged bridge configuration, and the
   Finished HTTP status. All three timestamps must be inside dispatch time through
   dispatch time plus 195 seconds, with both log timestamps at or after that
   attempt time. Logging timestamps may arrive out of order; their relative order
   is not an execution-order guarantee. Later natural attempts cannot supply this
   terminal evidence.
   HTTP 2xx proves successful transport; missing HTTP status remains unknown.
   The optional job status is auxiliary: an empty `{}` does not require a
   fabricated status message. See the official [Scheduler Job definition](https://docs.cloud.google.com/scheduler/docs/reference/rpc/google.cloud.scheduler.v1#job)
   and [execution-log guidance](https://docs.cloud.google.com/scheduler/docs/troubleshooting).
   Independently validate the exact new ordinary report and qualified account
   facts. This receipt always leaves `facts_verified` and `published` false.
   Source preparation and publication remain separate existing operations. Raw
   reports, resource mappings, group data and tokens stay in the protected
   runner; only fixed checks, hashes, timestamps, counts and result enums are
   emitted or uploaded.

The reviewed source runs strategy work synchronously, unwinds/disconnects on a
local deadline, and has no strategy thread surviving the response. With the
checked single-instance/single-concurrency configuration, ordinary same-service
requests serialize; this does not require all other Gateway clients to be idle.
The caller shares the existing configured client ID. Its **one HTTP attempt**
does not mean one internal broker connection attempt: the old deployed source
uses its original connection-retry configuration. The receipt keeps broker
connection count unknown. Success has no success notification; a service failure
may send the existing error notification, which this caller does not suppress.

The native group reader observes the currently declared configuration, including
a declared Secret's current version. It cannot prove that a long-lived worker
has reloaded a Secret changed since process startup. Keep the original group
configuration unchanged for this operation; do not present metadata or this
receipt as proof of loaded-process settings or independent release approval.


The source modes are separate, explicit choices. `deploy_receipt` retains the
original complete workflow/job/step/log/digest/source/target checks; expired or
unavailable logs stop that mode. It never falls back to static evidence.
`static_source` invokes the existing bounded OCI reader inside the same
preflight, bracketed by the caller's complete current-state reads. It requires
`static_files_match`, the exact reviewed candidate, every fixed source file,
immutable image/content integrity, all scope equalities, installed QPK/UES
metadata matching the candidate lock, a stable serving revision, and the image
command comparison all to pass. Missing fields, including an old eight-file-only
result, fail admission.

The fixed static list now includes the original eight files plus
`application/http_routes.py`, `application/ibkr_portfolio.py`,
`application/account_facts.py`, `application/runtime_deadline.py`,
`application/rebalance_service.py`, `application/monitor_dispatcher.py`, and
`runtime_config_support.py`. The verified OCI config's `Cmd` must exactly match
the reviewed single-worker/single-thread gunicorn command, with no image
`Entrypoint`. The diagnostic emits only `image_command_matches_reviewed`, never
command text, image environment or other image configuration. Existing layer,
overlay, digest, dependency and resource-budget checks remain in force.

This static comparison establishes only the selected file contents, image
command and dependency metadata in the immutable serving artifact. It does not
verify the process's loaded modules/settings, change risk admission or grant
independent release approval; the caller keeps those states unknown. Qualified
account facts and actual publication still require their separate original
report and receiver checks.
