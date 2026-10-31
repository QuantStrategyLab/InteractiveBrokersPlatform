# Daily strategy risk state

The strategy runtime accepts an optional caller-supplied daily state binding. The
binding identifies the strategy, logical account scope, frozen candidate and
configuration, durable state store, effective session, frozen input digest and
expected predecessor session. A first transition additionally requires a
bootstrap binding independently authorized by the caller. This interface does
not derive these values from runtime configuration or establish authorization.

The runtime reads and validates the scoped chain and confirms a proposed state
transition before using the persisted head. Missing, malformed, conflicting or
unavailable state cannot authorize a new risk increase. Without an execution
fence that binds the state and intent to broker submission, state enrollment
preserves independently valid reductions and does not authorize risk increases,
even after the state transition is persisted. Default runtime configuration does
not enroll this binding. Configurations using a distinct volatility exit
threshold and partial retention require durable state enrollment before relying
on stateful increases; no migration is implied by this interface.

An explicitly supplied binding requires the supported daily state strategy
contract and the `tqqq_growth_income` entry point. Other profiles are rejected
before strategy evaluation; they do not consume this TQQQ-specific state.
If the installed strategy package lacks that capability, evaluation
stops before calling the strategy. Passing an unavailable-state marker to a
legacy entry point that does not interpret it is not a valid fallback. The
default path without a binding does not import the candidate contract.

Each cycle uses one UTC whole-second decision clock for supported plugin and
risk-gate checks. The trading session remains a separate value. Independently
supplied expected artifact digests are matched to exact references; absent
bindings remain unverified. The decision receipt records actual pre- and
post-gate target digests when the shared runtime supports them, and a separate
partial link records an actual execution receipt when available. An artifact
read time does not prove that it was published or available before the decision.

The execution link may include a digest of broker-identified order records
observed in the completed strategy cycle. Missing, incomplete, duplicate,
unknown-outcome and dry-run records remain unknown. An order acknowledgement
or a filled-order status is not an individual fill: fill evidence is included
only when explicit, uniquely identified execution records are present. The
link remains partial provenance and does not prove end-to-end causality.

### Enrolled account journal submission

The optional joint path requires an independently enrolled account journal and a previously established equity baseline. The caller supplies a canonical account snapshot and separately approved per-order risk facts keyed by the journal order digest. These inputs do not authenticate their own origin. The portfolio consumed by the strategy must match the account observation in account, source digest, currency, valuation time, equity, cash and complete position quantities. Missing or inconsistent evidence blocks submission. The context digest is checked against the authoritative strategy transition before planning and again before reservation. The mapped allocation is bound to the gated decision and checked at the submission boundary.

The journal serializes state changes and order admission; pending or unknown outcomes remain blocking across session reconstruction. It cannot make broker submission atomic or establish missing broker fills. Enrollment, account identity, source verification and policy approval are external prerequisites. If a subsequent account risk adjustment changes the mapped allocation, the current binding denies that submission and requires a new evaluation.

Partial fills retain the unfilled order obligation. Unknown reports and
transport exceptions retain the original reservation and prevent another
submission after session reconstruction. Tests exercise the platform submit
wrapper with synthetic broker responses; they do not establish live broker
reconciliation or simultaneous TQQQ and SOXL operation in a shared account.

The option submission loop uses the same unknown-outcome stop as the equity
loop. An unrecognized or missing report is recorded against the original
option intent before notification formatting, and no later option intent is
submitted. A previously halted cycle cannot enter the option loop. The final
summary preserves `broker_order_outcome_unknown`; it does not recast uncertainty
as an ordinary pending acknowledgement or estimate proceeds from that report.
The current rebalance entry point rejects every executable option plan because
final RiskEngine order limits are unavailable. Combined equity and option
buying-power use is therefore not accepted by this entry point. The regression
tests exercise the offline loop and its durable journal; they do not change
that admission rule or enable option trading.

## Captured source-field evidence

`scripts.historical_source_evidence.validate_source_evidence(document)` checks
already captured, sanitized observations offline. It imports no application or
runtime package, reads no files, and performs no network or broker operation.
The runtime does not call this function. The contract is separate from state
coordination, historical replay and account-journal admission.

The document schema is `qsl.ibkr_source_evidence.v1`. Each source envelope keeps
the original producer field names in `payload`; `normalized` contains the
intake projection. A normalized field cannot supply a missing native source
field. `evidence_kind` must distinguish `synthetic` from `historical` material.
Neither value authenticates the observations.

| Input | Required evidence and local checks |
| --- | --- |
| `source` | A sanitized `runtime_report.v1` payload, opaque `source_id`, capture time, and a declared immutable locator consisting of `object_alias`, `version` and `raw_bytes_sha256`. |
| Source digests | `digest_domain` is `sanitized_json`. `payload_sha256` is recomputed over sorted, compact, ASCII-escaped JSON with non-finite numbers forbidden. The original byte digest remains a separate declaration; the function cannot recompute it from sanitized JSON. |
| `source.producer` | Forty-character platform and QPK source revisions plus the reviewed producer file paths and SHA-256 digests embedded in the validator. An absent or unrecognized source version is blocked. Matching file digests do not establish which version was deployed. |
| `source.sanitization` | A `qsl.ibkr_source_aliases.v1` namespace and explicit substitutions for the native run ID and three account-selector observations. Each substitution identifies its source pointer, stable alias and original-value digest. The supplied mapping must be internally one-to-one. |
| `account_binding` | A separately located, digest-bound `qsl.sanitized_account_binding.v1` observation, referencing the report ID and sanitized digest. It declares the account domain and an effective interval with an exclusive end. This is an intake schema, not a native runtime-report field. |
| `normalized` | The report and binding digests, profile, run alias, logical account scope, deployment revision, complete release identity, clocks, execution lane, record kind and unchanged execution receipt. `field_sources` must match the fixed source pointers; the document also carries `normalized_sha256`. |
| Optional `daily_projection` | A separately located source from the reviewed daily producer, with a source reference in `normalized.daily_projection`. Coverage and individual fills remain `UNKNOWN`. |

Physical account identity has five dimensions: `platform`,
`execution_environment`, `server`, `lifecycle_id` and `account_alias`. The alias
namespace includes the broker, physical execution environment, server and
lifecycle. The account selector, reporting account list and
`summary.account_facts.account_ids` must each contain the same single alias.
The account snapshot must identify `ibkr_account_snapshot.v1`. A logical
`account_scope` is insufficient, and a Gateway host is not a physical-server
binding. Accounts at other brokers, on different servers or in different
environments remain separate. Matching aliases and a supplied effective interval
do not authenticate a real account or establish shared-account acceptance.

The reviewed `RuntimeTarget` producer retains a legacy `execution_mode` derived
from `dry_run_only`: it uses `paper` for a local dry run and `live` otherwise.
That label does not establish the physical broker environment. This contract
requires the separate `execution_environment` field, the reported Gateway mode
and the declared account domain. A broker-paper observation can therefore retain
the native receipt's legacy `live` label while its physical environment is
`paper`. Missing explicit environment evidence is blocked. `dry_run` remains a
separate execution lane; maintenance observations, diagnostics and paper-command
reconciliation remain separate record kinds. A generic runtime report does not
by itself attest that a strategy execution occurred.

The whole-second decision clock is established before report construction and
may precede the report start by an initialization delay. The reviewed producer
uses the report start as the loaded-release timestamp. Checks retain that order,
the start-to-finish interval, account-observation bounds and capture-time upper
bounds. Receipt clocks retain their native whole-second precision. A terminal
fallback receipt can be created after report finalization; its clock association
remains `BLOCKED` because native receipt fields do not identify that path.
Capture time is not evidence that a plugin or other input was published before
the decision.

`qsl_execution_receipt.v1` has no native run ID, order list or individual fill
records. The validator preserves its original nine-field shape, verifies its
canonical receipt digest and compares its profile, strategy revision and legacy
mode with the containing report. A parent-report reference does not add a run
ID to the receipt. Acknowledgement and filled-order outcomes cannot become
individual fills, and a known unknown cycle cannot become an acknowledged
receipt. Normalized `individual_fills` and `pending_orders` must remain
`UNKNOWN`; omitted arrays cannot become zero activity.

The current daily projection retains `coverage_unconfirmed`, an incomplete
target record and `fills = {source: "not_connected", records: [], count: null}`.
Its twenty-report bound does not establish archive coverage. Its opaque run IDs
cannot be recomputed from a sanitized report's run alias; association with the
original run remains unverified. The validator neither supplies missing runs nor
creates original state observations. The runtime report does not retain the
complete `input.state` and state-transition sequence needed for cross-run state
continuity, and SOXL has no state adapter in this contract.

The result always has `status` equal to `BLOCKED` or `FAIL`.
`local_consistency` is `CONSISTENT` only when the supplied observations match
this narrow contract; missing or unsupported evidence is `BLOCKED`, and checked
contradictions are `FAIL`. Independent checks continue after an unsupported
producer so that it cannot hide a separate observed conflict. Findings contain
stable codes and scopes without source values.

`execution_authorized`, `production_ready` and `historical_parity_established`
are always false. Fixed gaps retain original-source and alias authentication,
deployment provenance, source availability, complete history, plugin payloads,
individual fills, pending account state, state continuity and decision-to-order
causality. Caller fields such as `verified` or `complete` do not remove these
gaps. Synthetic tests establish only these offline checks; they do not validate
historical records, production behavior or a shared account.
