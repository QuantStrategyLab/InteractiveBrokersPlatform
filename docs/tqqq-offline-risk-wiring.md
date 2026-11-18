English | [简体中文](tqqq-offline-risk-wiring.zh-CN.md)

# TQQQ Risk Policy, Funding Contract, and State Journal Offline Wiring

`interactive_brokers_platform.application.tqqq_offline_risk.run_tqqq_offline_risk` wires a synthetic TQQQ
account contract into the existing UES risk and settings-draft chain. It exposes
four actions only (`initialize`, `read`, `persist`, and `preflight`) and is
never registered in `main`, the composer, production strategy dispatch, or order
entry points.

This is strictly an offline adapter path. Production
`_build_runtime_risk_capabilities` dispatch for SOXL, production QPK/UES
dependency pins, deployment manifests, and live configurations stay unchanged
without turning this path into an approved live TQQQ runtime or copying SOXL
reserve ratios or exit buffers.

## Reused Capabilities

- Existing `build_ibkr_account_facts` produces frozen snapshot facts;
  `LoadedStrategyRuntime._build_capital_base_capabilities` verifies net
  liquidation value and the capital denominator while the cash adapter
  distinguishes raw, settled, and spendable balances.
- UES cash, fee, price, quantity, and three-tier funding adapters consume
  synthetic inputs; `preview_capital_adaptive_target` validates frozen funding
  policy and Kelly QP generates candidates without adding new solvers, fitting
  models, requesting quotes, or computing historical returns.
- Existing `RiskEngine` and stoploss/circuit-breaker/recovery policy gates handle
  risk events; `PortfolioEventJournal` and QPK create-only stores persist state;
  settings-draft I/O saves, reads back, and preflights complete drafts.
- Single-strategy inputs may invoke the same research candidate path (empty
  strategy sets still reject) without disguising defensive holdings or cash as a
  second strategy.

## Required Caller Inputs

1. `runtime`: a dry-run, cash-only, options-disabled synthetic
   `LoadedStrategyRuntime` with `tqqq_growth_income` profile and consistent
   target, account scope, account list, and execution cash reserve ratio. Fixed
   cash reserve floor must be supplied explicitly matching runtime so the
   funding contract covers `max(ratio * equity, floor)` and deducts reserves
   once; unset values never default to zero.
2. `portfolio_snapshot`: synthetic native-shaped snapshot whose `packet.facts`
   matches production projection output; explicit all-cash synthetic cases may
   attach `inventory_evidence` to use the research empty-inventory projection
   after independent completeness verification (real broker facts and gaps must
   be verified in protected records). Native options inventory must be
   explicitly empty rather than masked by stocks-only projection, cash evidence
   must align with `cash_balances`, and settled cash requires native
   `SettledCash` or bound synthetic Web ledger evidence rather than `CashBalance`.
3. `packet`: existing UES `facts`/`account_contract`, `cash_evidence`,
   `execution_rules`, `moments`, `composition`, `risk_inputs`,
   `definition_bytes`, `request_id`, and Kelly math contract for the single TQQQ
   strategy composition.
4. `event_context`: full three-tier event policy, risk observations, start-of-day
   baseline, external flows, high-water mark, post-fee holding period, and
   applicable reduction/recovery evidence. Missing budgets, horizons,
   thresholds, curves, or rules surface as explicit gaps.
5. `journal`, `binding`: explicit offline root, `PortfolioEventJournal`,
   external exact checkpoint, and (for `initialize`) explicit initialization
   proof bound strictly to the active account scope, TQQQ composition, and event
   policy.

All sources enforce `synthetic=true`: real account data must never be relabeled
`synthetic` for research entry, and verified live statement provenance alone
never proves complete positions, cash obligations, settled cash, fee schedules,
or risk-policy eligibility.

### Preserving and Read-Only Checking of Native Cash Fields

Account snapshots optionally retain `broker_cash_observations`
(`ibkr.cash_observations.v1`), recording `CashBalance`, `TotalCashBalance`, both
`LEDGER` balance fields, `SettledCash`, `AvailableFunds`, and
`FullAvailableFunds` separately from `cash_balances` without summing them or
relabeling `AvailableFunds` as settled spendable cash.

Attachments inherit the parent snapshot's account identity and `observed_at`,
recording explicit currencies only (excluding `BASE`), preserving zero and
negative values, recording invalid values by fixed field name only, and omitting
empty arrays for persistence normalization compatibility; absent fields never
prove zero balances, empty obligations, or complete queries.

Normal and paper reconciliation snapshots capture allowlisted keys from
`accountValues()` prior to float coercion and same-key overwriting. Valid
decimals keep exact precision, malformed strings become invalid markers without
storing raw text, duplicate `(account, currency, tag)` entries record ambiguity
even when equal, and values exceeding 4096 characters are marked invalid rather
than truncated. Legacy snapshots without per-item attachments project from
`cash_balances` only without recovering lost precision or duplicates; mismatched
account identities reject the account facts.

Snapshots also optionally capture raw `SettledCashByDate`, `SettledCashByDate-C`,
and `SettledCashByDate-S` strings in `broker_cash_schedule_observations`
(`ibkr.cash_schedule_observations.v1`) alongside explicit currencies and `BASE`,
preserving duplicate rows and omitting `raw_value` (never truncating) if blank,
non-string, unencodable, or over 4096 UTF-8 bytes. Read-only source diagnostics
emit only fixed statuses (`OBSERVED_RAW`, `RAW_NOT_PRESERVED`,
`AMBIGUOUS_ROWS`, `MISSING`) without dates, amounts, or raw text in public
logs; this attachment never substitutes for native `SettledCash` or alters
spendable-cash selection.

Three-tier offline cash preflight checks both the numeric cache and per-item
captures, rejecting initial states or drafts on conflicts, invalid raw values,
or duplicates even if reconstructed facts appear consistent. Protected
`--broker-contract-source-evidence` reports `OBSERVED`/`INVALID`/`MISSING` per
field (`NOT_REPORTED` for older reports) while keeping
`spendable_cash_verified=false`, `complete_cash_obligations_verified=false`, and
execution gates unchanged. See
[IBKR TWS Account Value Keys](https://www.interactivebrokers.com/docs/tws-api/doc/account-portfolio-data/account-updates/account-value-keys)
and [Account Summary](https://interactivebrokers.github.io/tws-api/account_summary.html).

In **this offline path**, `runtime.runtime_settings.trusted_runtime_risk_policy`
consumes UES event-policy shape matching `event_context.policy` rather than
reusing SOXL `RuntimeRiskLimits` dicts; event stoploss/circuit-breaker rules and
`risk_inputs` funding budgets/curves remain checked separately.

Offline targets pin explicit frozen release references:

- `strategy_revision` must match the required research UES revision and
  installed QPK/UES VCS revisions.
- `config_sha256 = digest(runtime.merged_runtime_config)`.
- `risk_policy_sha256 = digest({"event_policy": policy, "funding_inputs_sha256": _hash(packet["risk_inputs"])})`.

These frozen references identify offline inputs only without issuing live
releases or proving policy publishers; input changes require updated references.

## Funding Rules and Settlement Provenance

Broker account capability, strategy funding rules, and spendable cash are
separate. A Margin account grants no borrowing permission or rule switch:
production cash-only paths cap buying power by native cash (`CashPolicy`:
`cash_only`, `reserved_cash_ratio`, `reserved_cash_floor_usd`) without making
non-borrowing claims across unsettled periods.

This research chain uses
`SETTLED_MINUS_DISJOINT_RESERVATIONS_AND_OBLIGATIONS_NO_SELL_BRIDGE`: spendable
cash equals settled cash minus disjoint reserves and other obligations,
excluding expected sale proceeds (`borrowing_allowed=false`,
`sale_proceeds_bridge_allowed=false`, explicit `settled_cash_source_field`).

`packet.cash_evidence` may include `web_api_ledger` using the 11-field source
header plus:

| Field | Contract |
|---|---|
| `read_status` | `RESOLVED`; unknown or read failures reject |
| `endpoint` | Exact `/portfolio/{accountId}/ledger` template without real account paths |
| `account_snapshot_sha256` | `digest(packet.facts)` bound to the current snapshot |
| `response` | Official per-currency response shape with required contract currency row (never inferred from `BASE` or other currencies/accounts) |

Selected rows require matching `acctcode` and `currency`, integer Unix-second
`timestamp` matching `observed_at`, visible/fresh validity at cutoff,
`cashbalance` matching native cash facts, and valid non-negative `settledcash`.
Any native `SettledCash` present in snapshots or attachments must agree without
conflict. The adapter internally populates `SettledCash` tagged
`source_field=IBKR_WEB_API_PORTFOLIO_LEDGER.settledcash` while retaining full
synthetic Web evidence in the packet (`TWS.SettledCash` stays unchanged on the
native path).

Web evidence never proves completeness of open orders, pending withdrawals, or
fee obligations; `pending_state`, `obligations_complete`,
`reservations_disjoint`, explicit `reservations`, and freshness checks remain
enforced across draft fingerprints and consumption cutoffs. Real Web capture
requires satisfying `accounts`/`subaccounts` prerequisites and protected storage
acceptance—see [One-time Client Portal ledger capture](web-ledger-capture.md) and
[IBKR Portfolio Ledger](https://www.interactivebrokers.com/docs/web-api/v1/endpoints/portfolio/portfolio-ledger).

### Per-Stage Usage Checks

| Stage | Required inputs and result boundary |
|---|---|
| State `read` | Trusted account/policy identity, checkpoint, and log chain only (never current cash, fees, or candidates); `OPEN` still blocks new risk |
| Baseline signals and commit-ban diagnostics | Checked against their own stage inputs; settled Web evidence is not a global prerequisite, and stage pass is not full funding approval |
| `initialize`, `persist`, `preflight` | Enforce full funding and risk admission; produce synthetic drafts and readback results only |
| Real risk increase or config adoption | Not granted here; requires separate live cash, obligation, active policy, and consumer verification |

## Generating Reserve Requirements from CashPolicy

Manual `reservations` remain supported, or callers may pass
`cash_policy_evidence` so QPK `CashPolicy` derives the reserve from loaded
runtime settings:

```text
CashPolicy(settings.reserved_cash_ratio, settings.reserved_cash_floor_usd)
  + config.execution_cash_reserve_ratio
  -> max(effective_ratio * verified_account_equity, fixed_floor)
  -> single RESERVE item
  -> SettledCash - RESERVE - disjoint OTHER
```

This evidence reuses the 11-field header (`synthetic`, `account_id`,
`scope_ref`, `currency`, `source`, `source_version`, `observed_at`,
`available_at`, `max_age_seconds`, `valid_from`, `valid_until`) plus:

| Field | Requirement |
|---|---|
| `read_status` | Explicit `RESOLVED`; unknown or read failures never default to zero |
| `resolved_cash_policy` | Three-field `CashPolicy.to_dict()` matching loaded settings |
| `config_sha256` | `digest(runtime.merged_runtime_config)` |
| `reservation_id` | Explicit caller ID for this reserve requirement (never matched by amount alone) |

Ratio mode does not require users to supply a floor; the resolved policy
captures effective defaults when explicitly defined while keeping raw unset
fields, removed overrides, or read failures distinct from zero. Amounts follow
the USD contract only. When `account_contract.reserved_cash` is provided, it
must match without overwriting explicit zeros or stale values; matching
`reservation_id` items attach provenance without double-deducting, and disjoint
`OTHER` obligations stay unchanged without mutating the input packet.

Full policy evidence enters the draft fingerprint, persistence, readback, and
consumption freshness checks; changes to non-binding floors still invalidate
prior drafts. `cash_policy_preview` returns `FROZEN_INPUT_NOT_SERVING_CONFIG`,
proving offline calculation only—never live consumption or adoption eligibility.
Both simple/advanced modes and all three tiers share this calculation, with
advanced `cash_floor_ratio` checked post-fee without extra cash deduction;
insufficient cash never triggers borrowing, external transfers, or sells, and
inflows never clear `OPEN`.

## State and Drafts

Isolated research dependencies include `ORDER_COMPONENTS_V1` synthetic order-fee
contracts (post-rounding per-share, per-order, min/max commissions, and explicit
surcharges) for cash and risk verification alongside fixed-BPS continuous
candidate generation, plus `PRO_RATA_MARKET_VALUE_SINGLE_ACCOUNT_ORDER` for
event liquidations (one sell order per symbol per account amortized across
holding periods without duplicate minimum commissions). Estimated fees remain
separate from broker actuals and unverified broker tiers/partial fills;
production pins stay unchanged.

`read` checks trusted account/strategy/policy identities, checkpoints,
timestamps, and log chains without consuming cash, fees, prices, curves, or cash
previews, returning `new_risk_prohibited=true` and zero
config/init/recovery/execution authority even when `CLOSED` is read.

### Binding Relative Risk Comparisons to the Same Offline Draft

`event_context.relative_risk_comparison` may carry explicit synthetic comparison
inputs using the 11-field source header plus `portfolio_id`, `request_sha256`,
`returns`, `binding`, `strategy_context`, `benchmark_context`, `allowed_excess`,
and `dynamic_policy` (`returns` holds equal-length `dates`, `strategy`, and
`benchmark` arrays of ISO dates strictly before the frozen cutoff date).

It reuses `preview_relative_risk` math while binding portfolio ID, request
digest, account scope, currency, net-wealth roles, and dynamic-policy tiers/NAV
across both simple and advanced modes. `persist` and `preflight` validate any
attached comparison while leaving `initialize` and `read` untouched.

Attachments may also include `account_drawdown_reference` (`benchmark_symbol`,
`reference_id`, `available_at_utc`, `frozen_at_utc`, `dates`, `net_returns`,
`context`) using strictly increasing daily net total returns prior to
`available_at_utc <= frozen_at_utc <= decision_cutoff_utc` matching
`strategy_context.account_risk_benchmark`, scope, currency, frequency, cost,
cash-return, and external-flow conventions.

Alternatively, `schema_version="qsl.security_drawdown_reference.v1"` replaces
`net_returns` with `returns` as a provider-adjusted close proxy (`context`
carries no account scope, declaring dividend, cost, and flow treatment per the
`qsl.security_drawdown_reference.v1` contract). Both source and account require
`valuation_sampling=DAILY_CLOSE` with matching currency and calendar; missing or
mismatched sampling rejects without inferring close sampling from date labels,
while older unannotated previews retain `valuation_sampling=null` without
upgrading to daily-close proof. Both attachment forms bind into the draft
fingerprint while keeping `enforcement_eligible=false`,
`reference_qualified=false`, and `relative_risk_enforced=false`.

Account risk references and unleveraged strategy benchmark mappings stay
independent without public defaults. QPK computes reference max drawdown (never
substituting single-year returns), taking `min` when both an explicit
`account_max_drawdown_fraction` and reference cap exist (and rejecting if both
are missing) without deriving excess allowances, active tiers, or curve
parameters. Observing zero drawdown or `WITHIN`/`EXCEEDS` never proves
`reference_qualified`, `model_calibrated`, or enforcement authority.

```python
initialized = run_tqqq_offline_risk(request, action="initialize", initialization=proof, **inputs)
# Persist initialized["next_binding"] explicitly; never guess latest head from directory.
saved = run_tqqq_offline_risk(request, action="persist", event_id=event_id, **inputs_with_checkpoint)
# Persist external checkpoint and saved["artifact_receipt"] separately.
read = run_tqqq_offline_risk(request, action="preflight",
    expected_artifact_sha256=receipt["artifact_sha256"],
    consumption_cutoff_utc=current_cutoff, **original_inputs_with_current_checkpoint)
```

Simple and advanced modes consume `mode=simple|advanced` and tiers
`conservative|balanced|growth` under identical constraints, keeping unconfirmed
fields `REQUIRED`. Capital-policy checks against `HOLD` ledgers do not emit
Kelly candidates or authorize new risk on existing over-limit holdings. Events
append monotonically and replay state without regenerating candidates or rolling
back saved circuit-breaker state when no qualifying draft exists; stale sources,
new `OPEN`, old heads, corrupt logs, or policy mismatches reject old drafts.

Empty-inventory research projection applies only with explicit empty
stock/option collections and fresh scope/digest-bound completeness proof; real
all-cash completeness remains an open gap, and unsupported nonlinear broker fees
cannot downgrade to fixed synthetic BPS rates.

## Verification and Scope Boundary

Isolated CI job `tqqq-offline-risk` installs pinned research overlays and fixture
sources only, failing on version mismatch, missing dependencies, or skipped
tests, while default CI checks that production entry points do not register this
adapter. See `.github/workflows/ci.yml` for full setup and pinned revisions.

Offline acceptance command:

```bash
IBKR_REQUIRE_TQQQ_OFFLINE=1 IBKR_OFFLINE_UES_SOURCE=/path/to/frozen/ues \
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv-offline/bin/python -m pytest -q \
  tests/offline/test_tqqq_risk_wiring.py
```

Tests use synthetic capital and scenarios only (covering three tiers, both
modes, three cash reserve modes, strategy floors, duplicate reserve IDs,
non-binding floor fingerprints, stale/conflicting sources, insufficient cash,
mismatches, unsettled cash conflicts, missing fee/quantity facts, unknown
pending state, `OPEN`, replay, inflows, and cross-process readback); fixture
numbers demonstrate code behavior only and are never recommended thresholds.

Scope ends at verifiable offline TQQQ wiring and rejection paths: real active
account policies, approved sources, fee/quantity eligibility evidence, trusted
initial/checkpoint issuers, storage qualification, and live consumers remain
separate prerequisites. Capital trade-offs, protected risk policy, and
broker-fact gaps must be confirmed in protected records; offline wiring and
drafts grant no execution or config-enablement authority.
