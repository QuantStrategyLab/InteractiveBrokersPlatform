English | [简体中文](ibkr-flex-ledger.zh-CN.md)

# IBKR Activity Flex Local Ledger Parsing

`application.ibkr_flex_ledger` converts a fetched IBKR **XML Activity Flex
statement** into minimal, provenance-preserving ledger facts.
`application.ibkr_flex_source.import_activity_flex_ledger` wires the existing
one-shot fetch, account-scope check, and parser into a callable import entry
point. Complete ledgers contain real amounts, account identifiers, and native
TWR, so they stay in approved cloud memory or existing private storage and are
never written to local logs.

`diagnose_activity_flex_ledger` is the source-side diagnostic entry point: it
calls the importer and returns only a `build_flex_ledger_diagnostic` status
report carrying status, method, period, currency, missing items, and safe
warning codes—never account IDs, valuations, fees, cash flows, or TWR values.
This projection does not indicate that return pages consumed or recovered data,
and does not connect to QRS balance snapshots, legacy QPK USDT interval
contracts, or any execution path.

`scripts/diagnose_ibkr_flex_ledger.py` is the executable diagnostic CLI. It
stays disabled by default and attempts one fetch only when
`IBKR_FLEX_DIAGNOSTIC_ENABLED=true`. When enabled, approved cloud Secrets or
protected configuration supply `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`, and
`IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON`. When disabled or incomplete, it makes no
network calls and emits fixed missing-config statuses. Exceptions print fixed
error codes without SDK exception bodies; Gateway credentials must never stand
in for Flex tokens, and stdout stays a sanitized diagnostic report.

## Inputs and Account Contract

```python
from application.ibkr_flex_source import diagnose_activity_flex_ledger

safe_status = diagnose_activity_flex_ledger(
    token=..., query_id=..., expected_account_ids=("UXXXXXXX",)
)
```

When downstream private cloud handlers need full ledger facts in approved
memory, call `import_activity_flex_ledger` and pass the full result only to
controlled private handlers rather than calling the status-report path as a
return consumer. Here TWR is account-level native period return rather than
strategy attribution; if UI pages display period returns, consumer contracts
must distinguish broker-native TWR from locally computed returns and never
substitute NAV differences or deposits.

The full `ledger` is private; authorized cloud callers may write only to
existing private report locations without creating new buckets, printing full
ledgers, or simulating real imports. Read-only and publish entry points below do
not establish that runs occurred or account data eligibility was granted.

### Read-Only Diagnostics and Single-Handle Collection

Manual workflow `diagnose-ibkr-flex-readonly.yml` leaves
`configured_account_coverage_only` off by default. When enabled, it requires
`read_once=true`, is mutually exclusive with cash and cashflow modes, and
rejects cashflow date ranges. Preflight requires complete primary and three
additional slot configurations while pinning expected accounts to primary without
granting new account permissions or editing the Query.

That mode generates one configured query request and polls the same handle up to
the bounded limit on `1019` without issuing per-account queries. In memory it
reuses one XML document with Activity statement, single-account, approved-set,
and row-account isolation checks, emitting only per-slot statement presence
(`primary`, `additional-1/2/3`), normalized dates, native section record
counts, and TWR field presence. Absent accounts emit `null` dates and TWR;
missing sections emit `null` counts; recognized native sections count known
records (possibly `0`); unknown child records are counted separately without
printing field names or values so non-empty unknown blocks are never reported as
empty.

When a nested row's account ID mismatches its parent statement, the diagnostic
returns `verify_report` with `nested_account_mismatch=true` and rejects the
entire statement without printing account IDs or record contents. Optional
expected periods are validated per present account. `coverage=not_evaluated`
stays fixed: field presence or row counts never prove returns are computable,
complete, or published, and nothing calls business POSTs or persists XML,
handles, or raw account IDs.

When statement account sets match but a row account mismatches, the full
diagnostic still fails; error receipts may include `cash_report_accounts` to
check cash-section accounts, periods, and native USD rows per slot, reporting
only slot names, dates, and ending-cash availability.

`scripts.diagnose_ibkr_flex_ledger` runs without the period-return publisher,
QRS target, source binding, or sync token. Calling it without flags keeps one
generate and one collect when `IBKR_FLEX_DIAGNOSTIC_ENABLED` is exactly `true`:

- `--preflight` validates only the three Flex settings, single target, six
  account-facts identity settings needed for primary binding, two optional
  protected scope settings, and expected period without creating an HTTP
  session, generating a statement, or publishing. Valid shape does not prove
  valid credentials, statement coverage, or financial facts.
- `--read-once` still requires the diagnostic flag. It sends one request and, on
  `1019`, polls the same handle every 3 seconds up to 5 additional attempts;
  other errors stop immediately. Publisher and diagnostic share one bounded
  collector. Exhausting attempts emits `pending` without storing private
  handles, regenerating requests, or replacing unconfirmed requests.
- `IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON` must contain one native account. Without
  both primary selector and additional targets config, scope stays exact-only;
  supplying only one, invalid JSON/shapes, duplicate accounts, or a primary
  mismatch rejects before HTTP. When both are complete, primary plus three
  additional slots pass to the source check while parsing primary only. Pure
  validation lives in `application.account_facts_target_config`, keeping
  publisher exceptions separate without importing publisher or QRS tokens.
- When set, `IBKR_FLEX_EXPECTED_PERIOD_FROM` and `IBKR_FLEX_EXPECTED_PERIOD_TO`
  must both be ISO dates with `from <= to`, matching the returned report period
  exactly. They verify responses only without modifying `SendRequest` dates or
  inferring today or yesterday. Unknown CLI arguments, bad scopes, or bad
  periods reject before HTTP without echoing inputs.

`.github/workflows/diagnose-ibkr-flex-readonly.yml` exposes manual
`main`-branch, attempt-1 execution only when `read_once=true` (default `false`),
injecting existing Flex Secrets plus `IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON`
and `IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON` into preflight and read steps
only (no credentials during setup). It shares a concurrency group with the
publisher so both cannot hit the same Query concurrently, and adds no publish
steps, QRS tokens, cloud identity auth, trading, scheduling, or raw artifacts.

Stdout stays fixed `ibkr_flex_diagnostic.v1`. `available` indicates only that
the target statement contains a qualifying native period TWR—not account fee
plans, settled spendable cash, security/fractional permissions, complete cash
obligations, or config eligibility. Raw XML, amounts, accounts, tokens,
`ReferenceCode`, and exception text are never printed.

### Inspecting Cashflow Source Fields Only

In the same read-only workflow, `read_once=true` and `cashflow_source_only=true`
run `--cashflow-read-once`. It reuses ledger cash-transaction/funds schema
statistics without invoking return or execution publishers, QRS sync tokens,
artifact writes, account-facts, or cashflow writes, and stops if combined with
`cash_source_only`.

By default it uses the configured query period. To inspect a historical window,
supply paired `cashflow_period_from` and `cashflow_period_to` ISO dates spanning
at most 31 calendar days; only in this mode are they passed to `SendRequest` and
verified against returned dates without modifying the Query template.
Conflicting `expected_period_from/to` bounds stop before HTTP, and ambiguous
`1019` outcomes never trigger replacement requests.

`ibkr_flex_cashflow_diagnostic.v1` returns statement summary, period, and fixed
field-level type, date/time, and timezone format counts without account IDs,
amounts, transaction IDs, notes, or dynamic attributes. Missing
`CashTransactions`, empty sections, and unknown child nodes are counted
separately, and date-only `StmtFunds` fields never fabricate midnight
timestamps. `coverage=not_evaluated`: present or empty rows never prove complete
cashflow coverage, unknown timezones never default to UTC, and dividends, fees,
or trade settlements never count as external inflows. See
[IBKR Cash Transactions field definitions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm).

### Inspecting Cash Report Fields Only

Setting `read_once=true` and `cash_source_only=true` runs `--cash-read-once`,
issuing one statement request and bounded polling on the same handle without
publishing. Default period-return diagnostic behavior stays unchanged, and TWR
is not required here; account scope, protected scope, optional expected periods,
and nested `CashReportCurrency.accountId` isolation remain enforced.

Output `ibkr_flex_cash_diagnostic.v1` contains the content SHA-256, period,
cash-field format counts, and `account_capabilities`.
`CashReport/CashReportCurrency`, `endingCash`, and `endingSettledCash` inspect
native `currency="USD"` rows only without converting `BASE_SUMMARY` to USD.
Missing sections, empty sections, absent USD rows, multiple rows, bad decimals,
and missing dates stay separate, and unknown fields count by total only. Amounts,
model names, and account IDs never reach stdout.

`account_capabilities` inspects `AccountInformation.accountCapabilities` within
verified accounts as `CASH`, `MARGIN`, `PORTFOLIO_MARGIN`, `MISSING`,
`AMBIGUOUS`, or `UNRECOGNIZED` without echoing raw text or inferring from
`accountType`, buying power, or asset totals. See
[Account Information definitions](https://www.ibkrguides.com/reportingreference/reportguide/account%20information%20trade%20allocations.htm).

Under [IBKR Cash Report definitions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm),
`Ending Settled Cash` is **statement period-end** settled cash. This diagnostic
fixes `coverage=not_evaluated`, `spendable_cash_verified=false`, and
`complete_cash_obligations_verified=false`, never defaulting missing data to
zero or replacing current cash snapshots.

The private ledger also retains `period_cash_facts` with period-end cash
values, account, currency, period, and statement digest. Only when one cash
section holds a single standalone USD row matching the native account, empty
`model`, matching from/to dates, and finite decimal strings for both ending cash
fields (plus matching `reportDate` when present) does it mark
`available_period_end`, preserving sign and decimal precision without requiring
trade-date cash to equal settled cash. Diagnostic output reports `period_cash`
status and missing codes only, without exposing private values or accounts.

`build_flex_ledger` validates account scope via
`ibkr_flex_source.verify_report_accounts` before parsing: the target account
must appear in exactly one `FlexStatement`, any extra statements must belong to
the explicit allowlist, and missing, unauthorized, extra, or duplicate
statements raise `FlexSourceError` without returning partial facts. A non-`AF`
`type` attribute is also rejected.

This contract targets **standalone USD accounts**: `currency` is declared
`"USD"` only when `AccountInformation.currency` is USD; otherwise it stays
`None` with `account_base_currency_not_usd`. Non-USD amounts stay in their native
currency without conversion or silent merging. The period-return publisher
separately retains `account_base_currency` (such as `EUR`) without relabeling
`currency=None` as USD.

## One-Shot Native Period Return Publishing

With `IBKR_FLEX_DIGEST_JOB_REFS` and digest storage identity configured, a
scheduled job reads USD period-end settled cash for the four digest accounts
from a separate Flex statement, verifying account IDs and identities via
existing Cloud Run digest tasks. Latest summaries keep their existing format
with amount, statement date, and source Flex SHA-256 metadata, while versioned
records write to `flex-cash/versions/<slot>/<statement-date>/<sha256>.json` in
existing private storage. Legacy overwrites save the prior summary with its
storage generation. Archives hold no raw statements or broker account numbers;
collection runs Monday–Saturday 18:23 SGT ahead of the 07:00 digest and shares a
query concurrency group with return jobs.

`application.ibkr_period_return.build_ibkr_period_return` extracts
`ibkr_account_period_return.v1` from a verified account ledger. It accepts only
a single protected expected account, valid matching statement/native return
dates, `return_assessment.computable=True`, method `native_ibkr_twr`, and source
`ChangeInNAV.twr`, requiring `percent` and `ratio` to differ by an exact factor
of 100 without substituting NAV deltas when native TWR is absent. Projections
include QRS contract fields only; account IDs stay in private sync requests.

`scripts/publish_ibkr_flex_period_return.py` is disabled by default and reads
configuration strictly from environment variables (never CLI arguments):

```sh
PYTHONPATH=. python -m scripts.publish_ibkr_flex_period_return
```

| Environment variable | Purpose |
|---|---|
| `IBKR_PERIOD_RETURN_PUBLISH_ENABLED` | Enables execution only when set to exact `true` |
| `IBKR_PERIOD_RETURN_TARGET_ID` | Approved QRS target |
| `IBKR_PERIOD_RETURN_ACCOUNT_SCOPE` / `IBKR_PERIOD_RETURN_ACCOUNT_KEY` | Approved private scope and receipt key |
| `IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON` | JSON array containing one approved native account |
| `IBKR_FLEX_TOKEN` / `IBKR_FLEX_QUERY_ID` | Native XML Activity Flex source credentials |
| `IBKR_ACCOUNT_FACTS_PROJECT_ID` / `_SERVICE_NAME` / `_RUNTIME_REVISION` / `_ACCOUNT_SCOPE` / `_DEPLOYMENT_SELECTOR` / `_ACCOUNT_SELECTOR_JSON` | Live identity fields used to derive primary source binding with the same algorithm as daily account-facts |
| `IBKR_ACCOUNT_FACTS_SYNC_TOKEN` | Reuses the existing private IBKR sync token; never shares Flex or control tokens |

Source binding is derived dynamically rather than pinned in
`IBKR_PERIOD_RETURN_SOURCE_BINDING_ID`, which goes stale whenever Cloud Run
revisions rotate and breaks `period-return`, `executions`, and `external-flow`
routes together. Derived `_ACCOUNT_SCOPE` and account must match period-return
scope and account, failing closed with `primary_identity_mismatch` otherwise.

Target, binding, scope, account, and receipt key must come from independent
protected config rather than inferred from imported statements. Once config
validates, `import_activity_flex_ledger` runs one `SendRequest` and one
`GetStatement` (with bounded same-handle polling only on `1019`); disabled or
incomplete config sends no Flex HTTP, and missing native TWR sends no sync POST.

### Pending Statements and Explicit Collection

Under IBKR's [GetStatement contract](https://www.interactivebrokers.com/docs/web-api/api-reference/get-statement),
parameter `q` reuses the `ReferenceCode` returned by `SendRequest`;
[error code 1019](https://www.ibkrguides.com/orgportal/performanceandstatements/flex3error.htm)
indicates generation is still in progress rather than requiring a new request.
The source API separates:

- `request_activity_flex_report(token=..., query_id=...)` — sends one
  `SendRequest` and returns `FlexReportRequest` holding token/reference in
  approved process memory (`repr`/`str` redact them).
- `collect_activity_flex_xml(request)` — sends one `GetStatement` for that
  handle; raises `FlexReportPending` with `.request` on `1019`, and fails other
  errors without retrying.
- `import_activity_flex_ledger_from_request(request, expected_account_ids=...)`
  — runs one explicit collection plus standard account validation and parsing;
  collection authority remains governed by authorization, not by the API.

Existing `fetch_activity_flex_xml` and importer paths retain one-generate,
one-collect semantics. Do not log, serialize, or pass handles through artifacts
or CLI arguments. When bounded polling in the publish CLI or `--read-once`
exhausts retries, the publish CLI exits `1` with `flex_pending` and zero POSTs,
while diagnostics emit `pending/flex_generation_pending` without regenerating
requests or rerunning across processes.

### Explicitly Configured Additional Account Returns

Optional Secret `IBKR_PERIOD_RETURN_ADDITIONAL_ACCOUNT_KEYS_JSON` maps existing
additional slot names to receiver account keys. Only listed slots publish;
omitting it preserves single-account behavior. Keys come from protected receiver
config rather than display names, statement order, or XML. Target, scope,
selector, and source binding reuse `IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON`
with `_bound_source_id`; invalid, duplicate, or primary-conflicting configs
reject before network requests and never appear in public examples, Git, logs,
or regular artifacts.

Optional `IBKR_PERIOD_RETURN_ADDITIONAL1_ACCOUNT_KEY` appends an existing
`additional-1` receiver key in process without rewriting JSON or other slots; if
both define `additional-1`, they must agree or preflight rejects.

When enabled, one fetched XML document is validated and projected per slot in
memory using the same `1019` wait handle. Primary retains returns, executions,
and external flows, while additional slots publish native TWR and qualifying
`nav_change` only—never executions, flows, or balance snapshots. Unlisted slots
gain no publishing eligibility even if present in the XML. ACKs are checked per
account: unavailable fields or unknown ACKs stop retrying that slot while
continuing other independent slots, whereas explicit HTTP failures or unknown
failure categories stop subsequent slots.

Historical backfills may select `additional_returns_only=true`
(`--additional-returns-only`), skipping primary, executions, and flows (mutually
exclusive with `external_flow_only`) while keeping the same query and optional
`report_date`. It does not backfill account-facts NAV snapshots or alter
trading, query templates, schedules, or authorizations.

`.github/workflows/publish-ibkr-flex-period-return.yml` provides manual
`main`-branch, attempt-1 GitHub Actions execution using the pinned uv setup and
frozen runtime lock, requiring both maintainer variable
`IBKR_PERIOD_RETURN_PUBLISH_ENABLED=true` and input `publish_once` (both off by
default). Secrets inject only into the publish step without GCP ADC/WIF/Gateway
credentials, trading edits, Scheduler edits, or notifications. Reruns are
disabled: on `pending` or unknown outcomes, stop and investigate rather than
dispatching a replacement run because no cross-process private handle store
exists.

The sync endpoint is fixed to `<console-url>/api/account-facts/period-return/sync`.
The publisher sends one POST with redirects and environment proxies disabled
and an 8192-byte response cap. Success requires HTTP `200` with an ACK
containing exact keys `ok/stored/unchanged/account_key/period/currency/method`,
valid booleans (`ok` and `stored` true), and exact matches against expected key,
period, currency, and method. Timeouts, `5xx`, oversized responses, or malformed
ACKs return `sync_unknown` and stop without retrying; `3xx`/`4xx` return
`sync_rejected`. Primary publishing also emits `period_return_http_status`,
`executions_http_status`, and `external_flow_http_status` (for received integers
`100`–`599`), per-endpoint `*_sync_reason` (`http_response`, `invalid_ack`,
`transport_error`), and per-slot `additional_return_sync_diagnostics` without
reading non-200 bodies or printing accounts, return values, tokens, or raw
exceptions. Exit codes: `0` for `published`/`unchanged`, `2` for
`disabled`/`configuration_incomplete`, and `1` otherwise.

This entry point updates only the latest native period per account without
generating daily return curves, triggering trades or notifications, or adding
storage. Local verification uses synthetic XML and mocked HTTP only and does
not prove live token/Query bindings or production recovery.

## Output Facts

The returned dictionary has `schema_version="ibkr_flex_ledger.v1"`:

| Field | Meaning | Source |
|---|---|---|
| `account_ids` | Unique account | `FlexStatement.accountId` |
| `account_base_currency` / `currency` | Observed / declared currency | `AccountInformation.currency` |
| `period` | Period start/end and `period` label | `FlexStatement.fromDate/toDate/period` |
| `ending_valuation` | Period-end native-currency valuation (row matching period end date only) | `EquitySummaryByReportDateInBase.total` |
| `external_flows` / `net_external_flow` | External inflows/outflows and total | `StmtFunds` `DEP` (positive) / `WITH` (negative) |
| `fee_lines` / `fee_total` | Fee details and total | `StmtFunds` (`MFEE`/`OFEE`/`FRTAX`/`STAX`/`TTAX`) plus `tradeCommission` on trade rows |
| `fx_rates` | Available exchange rates | `ConversionRate.fromCurrency/toCurrency/rate` |
| `native_returns` | Broker-native TWR text (`unit="percent"`) when present and period-valid | `ChangeInNAV.twr` |
| `return_assessment` | Computability, method, and missing items (`value` in `unit="ratio"`) | Local evaluation |
| `warnings` | Per-item data issues and contract warnings | Local evaluation |

Fees are read solely from `StmtFunds` (never stacked with
`CashReport.commissions` or `Trade.ibCommission`) to avoid double counting;
`tradeCommission` reads trade-row commissions disjoint from fee activity codes.

When external-flow rows reuse the same native transaction identity,
`net_external_flow` applies publish-projection account, period, currency, and
identity checks: exact duplicate identities count once, and conflicts or
unverifiable identities set the total to `None` with warning
`duplicate_external_flow_identity_unverified` while retaining raw
`external_flows` rows. Rows without duplicate IDs keep existing summation
semantics without implying completeness or buying power.

## Periods and Returns

A period is valid only when both `fromDate` and `toDate` exist with
`from <= to`. Native TWR requires one account-information row, one childless NAV
row, and native calendar dates matching the statement. Declared account, model,
and currency must not conflict with account-total scope; missing child
attributes stay compatible without forcing conflict, while multiple or
contradictory sources suppress native returns.

This check does not prove the query targeted full-account scope rather than a
model; missing `model` is not full-account proof, and `ChangeInNAV` does not
list `currency` in IBKR's schema reference so optional currency is not treated
as mandatory.

`native_returns.twr` retains broker-native **percentage** strings tagged
`unit="percent"` per [Change in NAV](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm)
("measures the percent return"). Missing or reversed dates record
`native_twr_period_unavailable`; mismatched dates record
`native_twr_period_mismatch`; both omit native returns.

When `return_assessment.computable=True`, `method="native_ibkr_twr"` and `value`
divides the percentage by `Decimal(100)` as `unit="ratio"` (for example
`twr="1.25"` -> `value="0.0125"`). Otherwise `computable=False` and `missing`
lists specific gaps (`native_twr_unavailable`, `return_method_not_wired`,
`ending_valuation_unavailable`, `external_flows_unavailable`,
`external_flows_incomplete`, `external_flows_mixed_currency`). The module never
invents TWR algorithms, flow timestamps, or NAV-delta returns.

Ending valuation accepts only the row whose report date matches the period end
date; otherwise `ending_valuation=None` with `ending_valuation_unavailable`.

## Multi-Currency and Malformed Data

- External flows and fees sum only when all items share one currency; mixed
  currencies set the sum to `None` (`external_flows_mixed_currency` /
  `fees_mixed_currency`) while preserving individual lines in native currencies.
- Non-finite/unparseable amounts, missing currencies, or missing valuations
  record entries in `warnings`; any malformed external flow sets
  `net_external_flow=None` and `external_flows_incomplete`.

## Prerequisites for Real-Data Verification

This module produces fact fields only, not a full reconciliation/return ledger.
Before verifying against live statements:

1. Enable an XML Activity Flex Query in Client Portal including Equity Summary,
   `StmtFunds` (`DEP`/`WITH` and fee codes), `ConversionRates`, and
   `Change in NAV` where native return is required.
2. Store the token only in that target's private Secret Manager and supply Query
   ID via target config; process raw statements and parsed output only in
   approved cloud memory or private storage.
3. Confirm with real statements that base currency is USD, `ChangeInNAV` dates
   match the statement period, `StmtFunds` codes cover real inflows/outflows,
   fees do not double-count across sections, and ending valuations reconcile
   with Gateway views.
4. Confirm protected QRS configuration and exact ACK behavior before enabling
   period-return publishing.

## Sources and Tests

XML tag/attribute names and `StmtFunds` codes
(`DEP`/`WITH`/`MFEE`/`OFEE`/`FRTAX`/`STAX`/`TTAX`) follow IBKR Reporting
Reference docs for [Statement of Funds](https://www.ibkrguides.com/reportingreference/reportguide/statement%20of%20fundsfq.htm),
[Cash Report](https://www.ibkrguides.com/reportingreference/reportguide/cash%20reportfq.htm),
and [Change in NAV](https://www.ibkrguides.com/reportingreference/reportguide/changeinnav_fq.htm),
cross-checked against public `ibflex` type definitions.
`tests/test_ibkr_flex_ledger.py`, `tests/test_ibkr_period_return.py`,
`tests/test_publish_ibkr_flex_period_return.py`, `tests/test_ibkr_flex_source.py`,
and `tests/test_ibkr_flex_period_workflow.py` exercise placeholder XML,
multi-currency non-merging, `1019` pending handle handling, default-off
switches, and workflow boundaries offline.

## Manual Configuration Shape Preflight

`PYTHONPATH=. python -m scripts.publish_ibkr_flex_period_return --preflight`
reads only the eight publish config settings in-process via `_config`
validation without generating/collecting Flex statements, opening HTTP sessions,
calling sync publishers, or reading Cloud Run, Secret Manager, or Client Portal.
Even when `IBKR_PERIOD_RETURN_PUBLISH_ENABLED=true`, explicit `--preflight`
returns before publishing while preserving default-off and `1019` stop behavior
on normal runs.

Stdout emits `ibkr.flex_configuration_preflight.v1` containing `schema_version`,
valid workflow `source_sha` (or `null`), overall `status`, and per-field status
(`configuration_shape_valid`, `missing`, or `invalid`) across the thirteen fixed
field names without values, accounts, tokens, returns, counts, or exception
text. Both `..._JSON` lists parse JSON shapes independently so malformed JSON
points to its own field. Exit `0` means all validators passed; missing/invalid
config or unrecognized flags exit `2` without echoing inputs. Valid shape does
not prove Flex credentials, Query coverage, matching sync tokens, approvals, or
live returns.

`.github/workflows/check-ibkr-flex-configuration.yml` provides manual
`main`-branch preflight using `contents: read`, the pinned uv setup, and only
the same eight Secrets plus workflow `source_sha` without Secret Manager API
reads or artifacts.

## Date-Level External Cash Flows

From the same protected Flex read, the publisher posts `StmtFunds` `DEP`/`WITH`
entries to `/api/account-facts/external-flow/sync`. Native account, currency,
amount sign, date, and `transactionID` must be valid; model sub-accounts,
identity conflicts, and malformed flows are excluded. References hash the
account and native transaction ID rather than statement row numbers or exposing
raw IDs; dividends, interest, fees, trades, settlements, and FX are excluded.

Contract `external_flow.v2` preserves `time_precision: date` and
`effective_date` (`coverage: reported`, even when empty) without fabricating
midnight timestamps, default timezones, intraday weights, or portfolio TWR. It
does not mutate balance snapshots or observed timestamps. `external_flow_only`
in the publish workflow limits publishing to flows across up to 31 days via
paired `flow_period_from/to` (mutually exclusive with `report_date`), sharing
concurrency, credentials, and bindings without retrying unknown write outcomes.

## Native Execution Source Read-Only Diagnostic

`diagnose-ibkr-flex-readonly.yml` exposes `execution_source_only` (default
`false`, mutually exclusive with the other three modes), reusing the same Query,
token, and protected account bindings without modifying the Query template. It
requires `read_once=true`, `execution_source_only=true`, and paired
`expected_period_from/to` (up to 31 days; used for both `SendRequest` `fd/td`
and strict period verification, leaving cashflow date inputs blank).

Matching CLI flags are `--preflight --execution-source-only` (offline config
validation) and `--execution-read-once` (requiring
`IBKR_FLEX_DIAGNOSTIC_ENABLED=true`) while preserving single-target,
approved-account, Activity statement, period, and per-row protections. Empty
`Trades` or missing comparable fields report observed shapes only without proving
completeness or zero trades.

Side effects include generating one temporary Flex statement, consuming API
quota, and parsing XML in runner memory; raw XML, native IDs, amounts, and notes
never leave process memory or upload artifacts. A run issues one `SendRequest`,
waits 1 second, and on `1019` polls the same reference every 3 seconds up to 6
`GetStatement` calls (7 total API calls max), failing immediately on other
errors, rate limits (`1018` does not auto-retry), expired tokens, mismatches,
bad dates, malformed XML, or statements exceeding 8 MiB / 10,000 rows without
saving handles.

Output `ibkr_flex_execution_source.v1` emits fixed counts, field presence, date
distributions, and similarity hashes across at most 32 economic groups keyed by
an ephemeral in-memory 256-bit HMAC domain that is never persisted or cross-run
linkable. `source_ref` holds the statement SHA-256 without linking individual
executions, and raw accounts, IDs, symbols, timestamps, quantities, prices,
fees, and free text stay hidden. `fee_comparison` reports `same`, `different`,
or `unknown`. Duplicate/conflicting native IDs, dot-suffix prefix candidates, and
`CA`/`CO`/non-zero `orig*` flags count separately.

`coverage=not_evaluated`, `publication_authority_granted=false`, and
`correction_resolution=not_applied` stay fixed: matching prices/quantities/times
never prove duplicates, and prefix families never prove Flex corrections.
Execution projections retain `SHA256(accountId|ibExecID)` identity; duplicate
identities merge only when projected fields and `tradeID` agree, disabling
statement execution projection on conflicts without partial publishing. On
failures, `FlexSourceError.safe_diagnostic` emits only closed-set endpoint,
category, HTTP/provider codes, fixed `failure_stage`, and six
`execution_source_*` codes (or `flex_import_failed`) without exception bodies,
class names, or handles.

Primary references:
[SendRequest](https://www.interactivebrokers.com/docs/web-api/api-reference/send-request),
[GetStatement](https://www.interactivebrokers.com/docs/web-api/api-reference/get-statement),
[Flex error codes](https://www.ibkrguides.com/complianceportal/flex3error.htm),
[Flex Trades fields](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm),
and [TWS Execution](https://www.interactivebrokers.com/docs/tws-api/ref/execution)
(treated as candidate hints only, not a Flex deduplication contract).
