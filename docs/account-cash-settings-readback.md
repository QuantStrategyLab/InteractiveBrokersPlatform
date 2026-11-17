English | [简体中文](account-cash-settings-readback.zh-CN.md)

# Actual Serving Revision Cash Settings: Read-Only Diagnostic

`scripts.diagnose_account_cash_settings` reads existing Cloud Run metadata to verify cash-configuration declarations on the revision actually serving positive traffic. It never calls service business endpoints, reads account reports, contacts the broker, resolves Secret payloads, inspects container images, publishes facts, mutates configuration, or clears circuit breakers.

## Sources and Boundaries

1. Expected project, service, account scope, account selectors, and deployment selector come from protected target configuration alongside explicit strategy profile and region. Expected accounts are never inferred from an observed revision. Function entry points normalize JSON arrays or tuples under the existing account-selector contract for exact comparison without resolving aliases or rewriting caller expectations.
2. Service metadata selects the sole revision receiving positive traffic; `latestReady`, `latestCreated`, and zero-traffic candidates are not fallbacks.
3. Container environment declarations on that exact revision must come from a single container and match the expected strategy, platform, and account/service scope. Both `RUNTIME_TARGET_JSON` and `QSL_RUNTIME_TARGET_JSON` are accepted when valid, and conflicting explicit values are rejected without picking a winner.
4. A second service read confirms the positive-traffic revision remained stable. Any failure, timeout, identity mismatch, or ambiguous traffic suppresses cash fields. Each metadata call uses a 30-second timeout with no automatic retries.

Declarations read from the actual serving revision still do not prove in-process consumption, settled cash, or complete risk policies. Output always keeps `runtime_consumption_verified=false`, `spendable_cash_verified=false`, `account_policy_bound=false`, `settings_applied=false`, and `execution_authorized=false`. Live declarations cannot be relabeled `synthetic` for offline calculations.

## Cash Fields and Override Precedence

Only four existing fields are inspected:

- `IBKR_RESERVED_CASH_RATIO`, `QSL_IBKR_RESERVED_CASH_RATIO`
- `IBKR_MIN_RESERVED_CASH_USD`, `QSL_IBKR_MIN_RESERVED_CASH_USD`

Both ratio and floor follow the parser's QSL override precedence. Empty strings fall back, whereas whitespace-only strings take precedence and fail as blank. Field states preserve `ABSENT`, `BLANK`, `EXPLICIT`, `SECRET_REFERENCE`, `INVALID`, and `DUPLICATE`, with explicit zero flagged separately. Secret references record only that a reference exists without guessing or reading values; unset fields are not coerced to zero, and ratio mode does not require a synthetic fixed floor.

Output reports fixed field states and selected variable names only—never numeric values, raw environment variables, account/service identifiers, Secret names, or exception strings. Because bare SHA-256 digests of low-entropy numbers are enumerable, numeric hashes are never emitted. Callers needing intent comparison may pass `expected_declarations` in memory to get boolean equality alongside states while keeping missing or unread references unknown. Revision digests identify observed metadata only.

## Using the Read-Only Identity Entry Point

In an environment already authorized to read Cloud Run metadata, reuse existing `IBKR_ACCOUNT_FACTS_*` target configuration and `CLOUD_RUN_REGION`:

```bash
python -m scripts.diagnose_account_cash_settings --strategy-profile <selected-strategy-profile>
```

Target parsing validates protected config shapes without listing or downloading reports. `diagnose_cash_settings` also accepts the same expected bindings programmatically without hardcoding mappings. CLI exit code `0` (`OBSERVED`) indicates metadata read and scope validation completed while keeping financial eligibility `false`; exit code `1` (`UNAVAILABLE`) must not be interpreted as defaults, an empty account, or a production outage. Existing workflows do not invoke this diagnostic automatically.

## Flow and State Continuity Remain Separate Evidence

Cash environment declarations contain no deposit/withdrawal history, intraday equity index, or trusted initial state. Native IBKR Flex TWR binds report date boundaries and cannot prove complete intraday external flows. QPK `cash_flow_adjusted_return` covers explicit end-of-period flow conventions, and daily interval contracts for other assets cannot be relabeled into USD intraday tracking.

UES journals prevent high-water marks from decreasing and keep `OPEN` from closing automatically, but existing journals do not store prior confirmed adjusted indices, adjacent observed NAVs, or snapshot digests, and input fingerprints cannot invert them. Missing trusted index sources, prior anchors, or exact intervals remain open gaps rather than synthesized indices; successful cash-setting readback does not establish state continuity.
