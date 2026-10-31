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
