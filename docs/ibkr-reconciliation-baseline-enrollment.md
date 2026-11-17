English | [简体中文](ibkr-reconciliation-baseline-enrollment.zh-CN.md)

# IBKR Legacy Live Baseline: Verification Material Generation

This workflow applies only to frozen baseline recovery for targets explicitly
placed in `RECONCILE_ONLY`. For targets still in `ACTIVE_LKG` with
`RUNTIME_TARGET_ENABLED=false`, an absent
`IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON` does not block ordinary `/run`
execution and does not require Flex tokens, manual statements, or commit-ban
diagnostics. Ordinary recovery follows the active baseline, single-target
config, and applicable account, order, and risk protections, inspecting
historical logs only for known exceptions without rewriting baseline state
first.

For disabled live targets, start by running `Inspect Historical IBKR Execution`
manually for the target and UTC date of any legacy marker. It reads private run
reports in the cloud and logs date, cycle status, submit counts, and existing
execution-confirmation categories without printing orders, account amounts, or
raw reports. `submitted/not_observed`, missing receipts, or missing reports
require further checking and never auto-approve recovery.

Historical inspection resolves service identity from protected
`CLOUD_RUN_SERVICE_TARGETS_JSON`; public manifests list target catalogs and
regions only and never supply production service names. Config must match a
single live target consistently before reading. Outer workflows pass repo
variables/secrets into protected callees that mask raw values before runner env
printing and separately mask resolved service names.

Execution-ledger summarization runs independently of broker sampling whenever
target, source, and cloud auth are ready, preserving broker failures rather than
equating ledger reads with cleared orders or resume permission. Enabling
`inspect_execution_ledger` reports bounded same-account ledger counts without
calling the broker; report read failures do not skip ready ledger checks.

`Probe IBKR Live API Permission` permits only a single live target in
`ACTIVE_LKG` with trading switches off and `IBKR_FORCE_RUN=false`. Using a
one-shot Cloud Scheduler identity, it calls internal `/live-permission-probe`,
atomically claims the request in private cloud storage, checks account and broker
`what-if` write access using the live Gateway config without running strategies
or placing orders, and disconnects without retrying or logging raw broker
errors. Failures, duplicate runs, disconnect errors, config drift, or revision
drift block enablement; success verifies API write reachability only—not symbol
permissions, available cash, or cleared legacy orders.

## Read-Only Open-Order Observation Contract

Pre-execution checks and reconciliation share `load_read_only_open_trades`,
which waits for `ib_insync`'s blocking `reqAllOpenOrders()` to capture open
orders across Gateway client IDs plus unconfirmed `openTrades()` entries without
double-counting the same `Trade` object. Never issue a request and read stale
cache, or treat missing APIs, empty response types, read errors, or timeouts
(10-second cap, preserving shorter deadlines) as an empty order book; restore
prior SDK timeout and error handlers afterward.

This point-in-time snapshot never subscribes to subsequent client orders or
binds, cancels, or takes over orders. An empty snapshot does not clear pending
records, execution claims, or circuit breakers; caches lacking terminal
confirmation continue blocking new buys until ordinary reconciliation verifies
them. Unverified open orders for other symbols on the same account block new
buys without guessing cash deductions; eligible reductions remain bound by open
orders, quantity rules, risk controls, claims, and submit authorization.

## Independent Broker Statement Source (Integration Pending, Off by Default)

`application.ibkr_flex_source` provides one-shot read-only XML Activity Flex
fetching and account-scope checks against IBKR's `SendRequest`/`GetStatement`
endpoints without following response URLs or HTTP redirects. Tokens come only
from authorized protected storage and never touch logs, CLI arguments, public
files, or candidate receipts; `1019` pending states poll the same handle up to
the bounded limit without regenerating requests, and other errors stop
immediately. Raw statements stay in approved memory or private storage.

Existing account-facts and return pipelines already store Flex Token, Query ID,
and protected account selectors. `Check IBKR Flex Configuration` validates
config shape only; `Diagnose IBKR Flex Without Publishing` with
`configured_account_coverage_only` reports fixed slot, date, and section counts
plus stock/period-end USD cash classifications and fixed reason codes without
comparing Gateway observations, generating expected digests, or proving complete
coverage or time alignment. `ACTIVE_LKG` targets do not depend on this
frozen-baseline path.

If a `RECONCILE_ONLY` target lacks required sections, inspect the existing
Activity Flex Query in Client Portal under the query owner's username and add
missing fields (such as Open Positions or Conversion Rates) while preserving
existing period, XML/date formatting, account grouping, and website
return/cashflow sections. Verify fields against this reference:

| Section | Fields to verify |
|---|---|
| Account Information | Native account identity, base currency, and statement period against protected selectors |
| [Open Positions](https://www.ibkrguides.com/reportingreference/reportguide/open%20positionsfq.htm) | Report date, currency, asset category, contract ID, quantity, and multiplier; verify Summary/Lot level to avoid double counting |
| Conversion Rates | Confirm "Include Currency Rates" is enabled; verify source/target currencies, rate, and report date |
| [Cash Report](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm) | Per-currency From/To dates, Ending Cash, and Ending Settled Cash separately |
| [Trades](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm) | Executions detail, native trade/order IDs, trade/settle dates, quantity, cash/commission currencies, and `Order Reference` |
| [Cash Transactions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm) | Date/time, currency, amount, type, and native transaction IDs without inventing missing IDs or time semantics |
| [Corporate Actions](https://www.ibkrguides.com/reportingreference/reportguide/corporate%20actionsfq.htm) | Report/event dates, contract, currency, type, quantity, and native `Transaction ID` for non-trade changes |

Missing XML nodes stay `unknown` rather than proving an option is unchecked or
positions/trades are zero; explicit empty sections still require checking query
filters and coverage. Period-end `Settled Cash` never replaces current spendable
cash or open-order obligations. Re-test via protected read-only diagnostics
after adjustments.

Consider a dedicated Query only when period, grouping, format, account scope, or
downstream template constraints conflict, because consumers currently have a
single `IBKR_FLEX_QUERY_ID` Secret slot and must not break website reports.
Never paste raw Query IDs or tokens into chat, PRs, or logs; manage templates
directly in Client Portal under the owning user (see
[IBKR Client Portal config](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration)
and [creating a Flex Query](https://www.interactivebrokers.com/docs/web-api/flex-web-service/client-portal-configuration/create-a-flex-query)).

Flex statements and live Gateway state are distinct broker views; statements
never guarantee inclusion of live open orders, missing facts stay `unknown`, and
observed digests are never copied directly as approved expectations.

`scripts/build_reconciliation_baseline_candidate.py` processes one or more
private sanitized `/reconcile` responses paired with saved source records and
independently verified `SourceReceiptExpectation` entries. Once
record/expectation fields and evidence members align, QPK produces
`broker_reconciliation_baseline_candidate.v2` requiring fresh receipts, matching
account/runtime, and reconciled accounting surfaces (without requiring a second
sample or fixed minimum interval). Obtaining independent expectations for a
first-time baseline remains an explicit wiring prerequisite; `False` comparison
does not prove ledger corruption.

The builder never connects to IB Gateway, touches raw account files, writes
Cloud Run env vars, edits `RUNTIME_TARGET_JSON`, or places orders; non-zero exits
indicate the candidate is ineligible for review rather than authorizing
recovery. Model reviews are advisory only (`unavailable`/`0` when omitted, never
forged or rewritten to `approved`), keeping human confirmation,
post-confirmation re-observation, five-digest comparison, and CAS/no-order
constraints intact. This tooling does not authorize live-account takeover, live
trading, order placement, or capital increases.

## Publishing Minimal Source Snapshots to the Unified Console

`scripts/publish_reconciliation_recovery_source.py` accepts candidates,
independent source files, and optional full reviews, re-verifying
record/expectation bindings, source roots, and evidence membership while
preserving manual confirmation/escalation boundaries regardless of model votes.

By default it only prints `qsl_reconciliation_recovery_source_snapshot.v1`
containing opaque recovery IDs, platform/strategy, candidate summary, sample
window, audit bindings, and stable block codes—no account numbers, positions,
cash, orders, fills, or five-digest values—and makes no Gateway, runtime, or
order calls.

Only when an explicit QRS HTTPS
`/api/internal/sync-reconciliation-recovery-source` URL and dedicated
`RECONCILIATION_RECOVERY_SYNC_TOKEN` are provided does it POST that minimal
snapshot to populate the manual confirmation queue without restoring
`ACTIVE_LKG`. Any downstream controller must re-read the broker, re-verify
sources and bindings, and use atomic compare-and-set; any failure keeps
`RECONCILE_ONLY`.

When given `gs://.../reconciliation-recovery/ibkr/source/...`, the publisher
writes the full candidate and optional review receipt (`null` if omitted) to a
private evidence bundle using GCS `if_generation_match=0` so existing objects
fail instead of overwriting, reading, or deleting.

## Private Verifier (Verify-Only, No State Writes)

`scripts/verify_reconciliation_recovery.py` uses a separate
`RECONCILIATION_RECOVERY_CONTROLLER_TOKEN` (distinct from the source publish
token) to fetch confirmed items read-only from QRS, re-verifies local private
candidates, independent source records/expectations, and optional reviews
(matching console sample counts to candidate members), reads a
**post-confirmation** `/reconcile` receipt, and verifies deployed
`RUNTIME_TARGET_JSON` still holds the exact `RECONCILE_ONLY` baseline.

It emits `ibkr_reconciliation_recovery_verification.v1` and a candidate QPK
atomic transition plan while fixing `controller_mode=verify_only`,
`no_order=true`, `execution_authority_granted=false`, and
`state_write_attempted=false`. Only a separate least-privilege controller may
consume that plan after re-checking all five digests and executing one exact CAS.

## Read-Only State Ledger Adapter (Off by Default)

To avoid copying full `RUNTIME_TARGET_JSON` blobs back into GitHub variables,
deployment can optionally read a private immutable ledger
(`ibkr_reconciliation_recovery_state_ledger.v1`) containing only version,
`recovery_id`, `service_name`, and the full QPK `transition_plan`—never runtime
targets, accounts, strategies, positions, orders, or execution permissions.

On consumption, the adapter derives the next target from the live target and
verifies unique service match, IBKR platform, `RECONCILE_ONLY` current state,
matching baseline ID and target fingerprint, and full QPK plan flags
(`no_order=true`, `execution_authority_granted=false`, CAS flags). The sole
allowed diff is `live_continuity.state: RECONCILE_ONLY -> ACTIVE_LKG` plus
injecting the five digests into `IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON`.
Missing fields, ledger replay, target drift, or multi-service ambiguity fail
closed; env entry points ignore ledgers addressed to other services while direct
app calls reject mismatches.

Workflows read the ledger only when
`IBKR_RECONCILIATION_RECOVERY_STATE_LEDGER_URI` is explicitly non-empty and
under the private `reconciliation-recovery/ibkr/state/*.json` prefix. When unset,
nothing downloads and live targets stay untouched.

First-time enrollment uses an off-by-default entry backed by protected
`IBKR_RECONCILIATION_RECOVERY_STATE_LEDGER_PREFIX`, generating unique object
names from verified source run IDs. After confirmation, it re-reads live facts
and confirmed revisions before writing the ledger create-only, while
single-target sync updates continuity state and expected digests conditionally
on Cloud Run `resourceVersion` (preserving `disabled` and other config, and
never retrying unknown writes or enabling trading). Confirmed ledgers retain
original confirmation timestamps without repeating confirmation solely for
engineering processing time, though adoption still requires a fresh read-only
observation within 30 minutes verifying identical account, target, five fact
groups, and stable query scope.

`scripts/publish_reconciliation_recovery_state_ledger.py` is a separate publish
adapter accepting only clean `ibkr_reconciliation_recovery_verification.v1`
outputs carrying `verify_only`, `no_order`, `execution_authority_granted=false`,
and `state_write_attempted=false`. By default it prints minimal ledger JSON
only, creating an object via `if_generation_match=0` only when given an explicit
state-prefix GCS URI without listing, reading, overwriting, deleting, deploying,
or placing orders.

## Fault-Injection Regressions

Recovery regression tests inject tampered console executable flags,
single-digest drift, expired confirmations, same-second confirmation/receipt
timestamps, non-`RECONCILE_ONLY` targets, and controller tokens aimed at
standard console routes. Every case raises or returns without a
`transition_plan` while asserting `state_write_attempted=false`.

## Collecting Candidate Receipts

Manual workflows support `inspect_execution_ledger=true`, reading private
execution markers and outcomes for the selected service under existing GitHub
cloud identity and logging counts of `execution_claim.v1`,
`execution_marker.v1`, `execution_outcome.v1`, and claims lacking outcomes
without uploading raw records or paths (failing on >256 items, read errors, or
identity mismatches). Legacy `execution_marker.v1` indicates local persistence
only and never proves broker fills or account takeover; claims without outcomes
require verification.

Internal `broker_reconciliation_completed` logs mark unconfigured expected
digests as `comparison_status=not_configured` while logging account identity
check results and counts for positions, cash, open orders, and recent fills
without emitting five uncomparable `*_mismatch` reasons. When a baseline exists,
logs report `matched`, `mismatched`, or `not_compared` per category (only
verified-scope comparisons claim `matched`/`mismatched`; currently post-request
open orders compare the point-in-time API snapshot while the other four show
`not_compared`). Counts never prove cleared orders, and five-digest checks
govern recovery candidates rather than ordinary `/run` cycles.

`Collect IBKR Reconciliation Evidence` is an explicit manual workflow. Because
Cloud Run services restrict ingress to internal traffic, it uses deployment
identity to schedule a run-scoped one-shot Cloud Scheduler job that invokes
`POST /reconcile` via the existing least-privilege Scheduler identity, extracts
sanitized `ibkr_reconciliation_candidate.v1` from the private run report
(retained 30 days as an artifact), and deletes the one-shot job on both success
and failure without calling `/run`, editing GitHub variables, publishing state
ledgers, or placing orders.

Workflow artifacts wrap `broker_reconciliation` inside
`ibkr_reconciliation_artifact.v1` so
`build_reconciliation_baseline_candidate.py` consumes it directly without
downloading full private run reports. One complete, trusted receipt per target
suffices; workflow success proves format validity only and properly stays
blocked when digests are unconfigured or ledgers diverge. Cash summaries choose
settled/ledger cash tags (such as `CashBalance`) rather than mark-to-market
`NetLiquidation` or margin buying power so unchanged accounts do not drift on
normal price moves.

## v2 Source Roots (Private Candidate Construction Only)

`application.broker_reconciliation_candidate` validates **at least one** saved
sanitized source record and produces
`broker_reconciliation_baseline_candidate.v2`. Each record strictly allows only
`schema_version`, `repository`, `workflow_path`, `workflow_run_id`,
`workflow_run_attempt`, `workflow_head_sha`, `artifact_id`, `artifact_name`,
`artifact_sha256`, `evidence_sha256`, `service_name`, `service_revision`,
`service_revision_commit_sha`, and `service_deploy_run_id`. The builder requires
matching counts, unique artifacts/runs, audit expectation matches (profile,
`main` success, artifact naming, consistent repo/workflow/head and
service/revision/commit), and writes the canonical root hash into
`source_receipts_sha256`, failing closed on missing, extra, or inconsistent
fields.

This module is an I/O-free private consumer: it never queries GitHub/Cloud Run,
starts workflows, touches brokers/accounts/orders, or reads/writes expected
digests or `ACTIVE_LKG`. All three CLIs require
`--source-records <private JSON list>` and
`--source-expectations <independently verified private JSON list>`
(`--dual-review` is optional for publisher and verifier) without downloading
sources or granting authority. Source roots bind content only and never prove
account identity, query completeness, accounting validity, or permissions.
Legacy v1 candidates stay read-only and are rejected for new requests.

## Verifying Account Bindings from Existing Reconciliation Receipts

In `Collect IBKR Reconciliation Evidence`, `inspect_flex_runtime_binding_only`
with a completed `reconciliation_run_id` reads existing same-repo workflow
artifacts inside a protected runner to verify the account set observed in
**that run** (or checks config only when left blank) without broker calls or
orders.

`evidence.account_scope_sha256` hashes deduplicated, sorted
`{"account_ids": [...]}` via `calculate_broker_observation_sha256`, comparing
against the matching Flex slot selector digest to emit
`observed_group_account_match` only—never leaking account IDs, digests, or raw
receipts outside the protected environment. Diagnostics verify workflow source,
status, run/attempt/head, artifact digest, target, Scheduler job hash, and live
metadata revision/deploy labels; unverified revisions or selectors remain
`unknown`. Equality proves only that specific run's account set matched the
slot—never current worker Secret versions, current balances, or reconciled
state.

## Looking Up Existing Account Snapshots by Strategy

`Inspect Account Facts Archive` accepts `global_etf_rotation` or
`russell_top50_leader_rotation`, uniquely matching an existing slot in protected
config by project, service, account scope, account selector, and deployment
selector and reading only that slot's prior report for the specified UTC date
(`day` mode only, allowing same-day reports for strategy mode while preserving
existing date limits elsewhere). Missing config, non-unique slots, read errors,
or identity mismatches fail or stay `unknown`.

Verified snapshots output only observed timestamp, cash record count, and native
USD cash source tags—never amounts, accounts, raw reports, or storage paths.
Historical revisions stay ineligible for current receiver admission, and field
semantics/time coverage still require independent verification.

## Cash Observation Provenance Scope

Account snapshots capture `broker_cash_observations` with cash values and source
tags from the SDK cache; `broker_cash_source_details` records per-field
provenance (whether read from `accountValues`, whether SDK carried a model code,
whether input was native text, and whether the key had no segment suffix
`-S`/`-C`) without printing model codes. `accountValues` is a cache whose
`observed_at` reflects read time rather than broker update time, is non-atomic,
and does not prove cross-date cash completeness. See
[IBKR Account Updates](https://interactivebrokers.github.io/tws-api/account_updates.html).

### Dual-Target Cash Read-Only Entry Points (Local Candidates)

`scripts/account_cash_readonly_entry.py` reuses existing maintenance collection
for `global_etf_rotation` and `russell_top50_leader_rotation` after trusted
preflight validates target identity, report prefix digest, active account
selector, profile, disabled status, and non-zero client ID. Standard SDK
initialization still reads positions and fills; reports write to private
temporary directories (cleaned on exit) and protected GCS paths, printing fixed
status only without equating write success to reconciled cash or live readiness.

`scripts/prepare_account_cash_job.py` reuses serving-target preflight and Job
builders to emit a private local build directory at a non-existent absolute
`CASH_CANDIDATE_DIR` (first emitting Dockerfile, pinned sources, and digests;
then, given immutable `CASH_CANDIDATE_IMAGE`, `CASH_CANDIDATE_JOB`, and initial
`CASH_BUILD_INPUT`, checking revision, target, prefix, base, file digests, and
`CASH_REGISTRY_ACCOUNT`). Stage 2 verifies base layers, full file tree,
permitted modifications and permissions, config, and local vs. registry OCI
manifest/config byte digests on a protected Docker host before generating
private Job definitions without building, pushing, or running Jobs. Build inputs
patch `main.py` for single-connection and maintenance-report isolation strictly
on verified source shapes.

The entry prints only the report URI's SHA-256 publicly, while protected
readback locates reports by unique Job, execution, isolated prefix, and object
generation without printing URIs or amounts.

`scripts/execute_account_cash_job.py` connects existing protected target
preflight, image verification, temporary Job lifecycle, and report readback. It
accepts one selected profile from an explicitly dispatched protected runner,
resolves the expected account from existing private target configuration, and
normally requires the original pre-build `CASH_BUILD_INPUT` for that target. It
reuses existing single-execution, ownership, cleanup, and before/after baseline
checks, accepting structured Cloud Run `jsonPayload` as well as text payloads
while verifying the exact execution and report generation. For existing
already-built candidates, `CASH_IMAGE_EVIDENCE_MODE=postbuild` skips pre-build
input by pinning the target's base digest and re-verifying base/candidate
images, source bytes, expected account, and serving revision (labeled post-build
verification). A protected workflow caller and real Job execution have not run
yet; shared Gateway client ID exclusivity (beyond separate workflow concurrency
groups), runtime identity, network, and storage permissions remain unverified,
and a current balance is never an expected value.
