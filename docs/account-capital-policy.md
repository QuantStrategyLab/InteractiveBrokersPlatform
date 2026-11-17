English | [简体中文](account-capital-policy.zh-CN.md)

# Account Capital Scope, Cash Reserves, and Scheduled Contributions: General Design

Status: offline cash-policy wiring for TQQQ/IBKR is implemented by reusing existing contracts; no accounts,
configurations, recurring transfers, or trading are enabled. Personal selections and real configuration values
are injected from protected sources; open-source code never assumes salary income, bank reserves, or a
specific risk tolerance.

## 1. Users Choose Account Capital Scope; the System Reuses Cash Rules

Users first select which accounts to manage. Each account keeps its existing `CapitalBaseBinding`,
source binding, and execution boundary.

| Case | Handling |
|---|---|
| All net assets of a selected brokerage account are managed | Use full-account scope and verified equity; the budget scales with real capital without manually re-entering fixed principal |
| Some cash or assets in the account are excluded from strategies | Use explicit scopes, reserve rules, or caps supported by existing capabilities; mark unsupported scopes unavailable rather than pretending full-account coverage |
| The account already has a cash reserve rule for living expenses and fee buffers | Reuse that rule to reserve once from strategy-allocatable cash; do not require a second budget for the same purpose |
| Living and emergency funds are held outside the system | May be recorded as a user declaration; never require importing bank accounts, salaries, or household bills, and do not deduct another emergency reserve |
| Scope has not been chosen or account sources are incomplete | Observe or explain previews only; do not assume all accounts or funds are authorized |

Managing all account capital does not require full investment in securities. Cash retained inside the account
remains account assets governed by the same capital rule. Covering all accounts does not allow moving money
between accounts or double-counting one physical account across multiple strategy budgets.

## Boundary Between General Settings and Personal Configuration

The open-source layer provides general capabilities such as three-tier and advanced modes, cash reserve modes,
config validation, risk calculations, and state persistence. Each deployer selects accounts, capital scope,
risk policy, and cash requirements through the existing configuration entry points. Personal risk numbers,
funding purposes, and approval records remain in private configuration or private discussion notes rather than
shared policies, public defaults, or test fixtures.

Personal profiles validate suitability for their specific instance only and cannot become assumptions for
other users or require salary, bank, or bill disclosures. When an account already configures a reserve for the
same purpose, reuse it without re-prompting or double-deducting. General engineering delivery, personal policy
completeness, and live configuration enablement are verified separately; an incomplete personal policy does
not block independent general engineering work.

## 2. Reusing the Existing CashPolicy

QPK `quant_platform_kit.common.runtime_control.CashPolicy` already defines `cash_only`,
`reserved_cash_ratio`, `reserved_cash_floor_usd`, `effective_reserve_ratio()`, and `reserved_cash_usd()`.
IBKR configuration, runtime, and execution already wire this contract without adding a second capital policy,
cash account, or reserve ratio.

```text
effective_reserve_ratio = max(account_configured_ratio, strategy_existing_reserve_ratio)
required_reserved_cash  = max(effective_reserve_ratio * verified_account_equity, configured_cash_floor)
```

The console supports three reserve modes, reused without synonymous options:

| Mode | User configuration requirement | Underlying field mapping |
|---|---|---|
| By ratio | Reserve a percentage of account equity | `reserved_cash_ratio`; clear user fixed-amount override |
| Fixed amount | Reserve at least a specified dollar floor | `reserved_cash_floor_usd`; clear user ratio override |
| Combined | Take the greater of ratio-based and fixed amounts | Supply both fields; keep the existing `max` rule |

UI controls to keep current settings or remove extra overrides are not fourth or fifth formulas. Every
option remains subject to the strategy's own minimum reserve; "fixed amount" sets a floor rather than forcing
final cash to equal that exact figure. UI mode, saved drafts, target fields, and effective values must be
displayed separately: field support alone does not prove mode selection, and an absent fixed floor does not
prove it was never set or is zero in production.

The persistence chain stores those two numeric fields without a separate runtime mode field. Ratio mode does
not require entering a fixed floor; defaults may be explained only where the source contract explicitly
defines fallback defaults, whereas read failures or unknown config layers stay unknown. Draft `null` override
removal must stay distinct from explicit zero.

Ratio and floor express two bounds on one cash floor via `max`, never by addition. Explicit zero values must
remain distinct from unset, unread, and invalid values; source defaults do not prove live account settings,
and deployment intent does not prove runtime consumption.

The TQQQ/IBKR path operates in USD and keeps those existing fields. Generic UI displays must show currency;
non-USD currencies cannot be passed into `_usd` fields by relabeling and must be handled by explicit platform
contracts and sourced FX, or marked unavailable.

The UI displays the effective ratio, floor, actual current requirement, source, and timestamp from one
effective policy. If advanced mode exposes `cash_floor_ratio`, document it as a post-fee portfolio cash ratio
constraint evaluated alongside funding availability checks, not a duplicate cash deduction.

## 3. Separating Allocation Equity from Spendable Cash

Allocation equity sets portfolio sizing; settled, spendable cash net of full obligations determines what can
be bought now. Stock market value, margin buying power, bank deposits, or future income cannot stand in for
settled spendable cash.

Reusing UES's cash-evidence bridge and account adapter:

```text
reserve requirement -> existing reservations evidence -> reserved_cash
spendable_cash = verified_settled_cash - reserved_cash - disjoint_other_cash_obligations
```

If a broker field already nets an obligation, verify definitions first and avoid deducting twice. Subtracting a
reserve from equity or `SettledCash` before passing `reserved_cash` causes double deduction. Adjusted
available amounts cannot be relabeled as raw `SettledCash`. Fees, open orders, unsettled activity, and other
obligations remain handled by funding checks; missing obligation evidence never defaults to zero.

One `reserved_cash` amount may cover declared living expenses and a fee buffer without creating two duplicate
reserve buckets or guessing a split. Knowing the purpose clarifies intent, but the effective ratio/floor
values, spendability, and suitability still require verification.

A fee buffer is a cash-planning arrangement whereas actual trading fees are transaction costs: reserving cash
does not pay fees, and actual orders still record modeled fees once in the cash ledger. The offline contract
requires purchase cost plus modeled fees not to exceed spendable cash after reserves and disjoint obligations,
while maintaining the cash floor post-trade. It does not split `reserved_cash` into consumable fees versus
locked living cash, so purpose declarations never relax the floor.

When reserve requirements exceed available cash, report the shortfall and block new buys without borrowing,
pulling bank funds, or forcing sales. Reductions and circuit breakers continue through the core risk engine
and approved rules.

## 4. Scheduled Contribution Plans vs. Settled Inflows

Recurring contribution plans represent future intent only; they do not authorize bank transfers or increase
current budgets or buying power.

1. When a verified source confirms cash arrived in the target account, record it as an external principal
   inflow.
2. Once funds become spendable under broker rules, recompute reserve cash and portfolio previews from the
   updated account snapshot.
3. Because account equity already includes settled principal, never add that inflow a second time as extra
   future cash.
4. Compute performance and drawdown using cash-flow-adjusted conventions: deposits are not returns, withdrawals
   are not losses, and high-water marks are not reset to erase prior drawdown.

Arrivals change equity and cash; loss limits follow approved formulas or protection curves without
automatically widening policy limits for new principal.

Reuse `extract_external_cash_flow`, `cash_flow_adjusted_return`, and the event/state journal. End-of-period
cash-flow conventions cannot claim intraday arrival precision; missing timing or continuity evidence remains
an explicit gap. Transfers between banks or other brokers must separate internal transfers from external
inflows within scope so total views never double-count principal.

Verified living-expense withdrawals from the account are external cash outflows rather than strategy
investment losses; check whether pending withdrawals overlap already-netted balances. Purpose labels on
`reserved_cash` do not prove a withdrawal occurred or authorize transfers or sales.

## 5. Three Risk Tiers Share One Capital Rule

Simple mode picks conservative, balanced, or growth; advanced mode edits exposed parameters only. Both consume
account scope, effective `CashPolicy`, quantity/fee contracts, and `RiskEngine`, with no bypass around cash
obligations or account risk caps.

Tiers change risk budgets and portfolio candidates without altering the user's cash floor. Allowing growth mode
to exceed a benchmark slightly requires a pre-defined comparison rule, bound, and horizon; "slightly exceed"
never becomes unbounded. Strict caps remain available as a reference. Adoption eligibility stays separate from
research observation: candidates failing an adoption threshold keep their research records, and frozen windows
are not altered to pass thresholds.

Max drawdown, volatility, and tail loss must state distinct roles and units. `protected_capital_basis` is a
curve reference rather than reserved cash, household emergency cash, or an extra deduction; personal loss
limits, percentages, and horizons still come from the user or effective policy.

General tier drawdown fields and personal approvals come from private risk-policy templates and private
sources rather than being published here. Approval of a candidate does not establish suitability for arbitrary
cash needs or replace curve calibration. Uncertain liquidity horizons remain uncertain; recurring income or
planned contributions never justify raising risk. Existing cash reserve rules remain reused without counting
future salary as buying power or deducting an invented buffer for unspecified needs.

## 6. Offline Wiring Scope and Completion Boundary

Only the TQQQ/IBKR offline preview entry point is wired, without rolling out across all platforms or adding a
new configuration framework:

- The offline entry reads the loaded runtime's reserve ratio, floor, and execution reserve ratio, injecting
  matching scope- and freshness-bound manual config evidence into `CashPolicy`.
- One reserve entry feeds the cash-evidence bridge across simple/advanced modes and all three tiers. Even when
  ratio-bound, changes to the fixed floor remain included in the persistence fingerprint and readback checks.
- Source, version, timestamps, config digest, and `reservation_id` persist with drafts and preflight checks;
  identical IDs validate without double-deducting, conflicts reject, and unmatched `RESERVE` entries never merge
  by amount alone.
- Missing real sources or financial thresholds surface as explicit gaps. Previews compute synthetic funding
  results only and never replace an active risk policy or produce deployable drafts.

Manual verification covers ratio-bound, floor-bound, equal-bound, explicit zero, insufficient cash, duplicate
reserve ID, disjoint order obligations, pre/post contribution inflows, duplicate inflow events, flow-adjusted
returns, and simple/advanced parity, ending at preview readback and cash-ledger conservation.

This interface is wired in [TQQQ offline risk wiring](tqqq-offline-risk-wiring.md). Raw balances do not
pre-deduct reserves; the account adapter deducts reserves and disjoint obligations once, and the trade cash
ledger records only security and fee flows.

Because this path does not query live serving config, readback marks config provenance as
`FROZEN_INPUT_NOT_SERVING_CONFIG`. Protected readback of live loaded settings, complete obligations, and
applicable fee/permission rules still requires separate evidence. Ratio mode may use resolved defaults only
where the resolution contract explicitly defines them.

A separate
[actual serving revision cash-settings readback diagnostic](account-cash-settings-readback.md)
inspects existing Cloud Run service metadata and environment declarations while distinguishing missing values,
explicit zeros, and unread Secret references without printing raw values or low-entropy hashes.

Contribution tests use explicit cumulative flows and caller-supplied flow-adjusted indices: new principal
updates NAV and `SettledCash` without counting as profit, repeating inflows, or resetting high-water marks.
Automatic intraday index generation from real arrival logs is not implemented, and no bank/broker transfers,
contribution schedules, or automatic risk expansions exist.

Offline wiring of cash rules does not establish real reserve values, complete obligations, active risk
policies, production consumers, or financial adoption.

Design references: [QuantConnect Position Sizing](https://www.quantconnect.com/docs/v2/writing-algorithms/trading-and-orders/position-sizing)
and [Investor.gov Asset Allocation](https://www.investor.gov/introduction-investing/getting-started/asset-allocation).
These provide background context rather than default user thresholds.

## 7. Drawdown Circuit-Breaker Risk Boundaries

Snapshot `drawdown_from_peak` and `peak_equity_usd` flow into the risk gate; sub-portfolios size using their
own `equity`, while account drawdown uses `account_equity_usd` rather than treating sub-portfolio equity as
account equity. Snapshot facts take precedence over stale strategy metadata projections, and invalid values
fail as bad inputs.

Native snapshot conversion populates `account_equity_usd` from verified `broker_net_liquidation` only when
`total_equity_source == "broker_net_liquidation"`; missing or unverified values stay unavailable and are
rejected by the risk gate without falling back to `total_equity` or strategy caches. Conversion preserves
explicit `realized_vol`, `observation_status`, `reconciliation_status`, `circuit_breaker_state`, and
`production_drift_status`.

When account drawdown trips, `CIRCUIT_BREAKER_OPEN` is appended to the physical account's
`AccountSubmissionSession` log, including cycles with no strategy targets. Subsequent cycles, other
strategies, and new processes read that same account log; equity recovery, explicit `CLOSED` records, or
caller reset flags never clear `OPEN`. No reset path is exposed; missing log capability, read/write failures,
conflicts, or readback failures block new risk.

When `PORTFOLIO_LOSS_STOPLOSS_ENABLED` is `OFF`, drawdown circuit breakers only block new positions and
additions; target scaling omits the drawdown component rather than trimming positions on half or full drawdown
thresholds. Capital and volatility scaling continue unchanged; when `ON`, existing scaling, trimming, account,
order, authorization, and `UNKNOWN` guards remain active. Switch defaults, risk parameters, and configurations
stay unchanged; the pinned QuantPlatformKit revision in `pyproject.toml`, `qsl.toml`, `uv.lock`, and CI
defines the active dependency baseline.

Live IBKR buy orders bind `new_risk=True` to `PREPARED` log entries and re-check `OPEN` before final dispatch.
Ordinary reductions remain governed by existing proofs and order guards; persisted `OPEN` never grants sell
authority. Dry-run paths without an account session return preview plans marked `NOT_PROVEN`. Unknown open
orders, stale or unknown observation status, unreconciled state, and invalid risk values block new risk and
cannot be overridden by healthy strategy caches.
