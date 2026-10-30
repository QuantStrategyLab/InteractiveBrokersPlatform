# InteractiveBrokersPlatform

## Account risk facts in runtime reports

When account facts pass the existing physical-account selector check, the report
may include `summary.account_facts.account_risk_fact_readiness` with schema
`ibkr.account_risk_fact_readiness.v1`. This receipt describes only the snapshot
report: `scope` is `SNAPSHOT_REPORT_ONLY`, `admission` is `NOT_EVALUATED` and
`status` is `NOT_READY`. It is not an execution decision or an authorization.
`snapshot_observed_at` is the capture time, not a verified broker freshness
watermark. Fees, quantity permissions, account domain and approved budget are
`UNVERIFIED`; the order observation watermark is `MISSING`.

`settled_cash` is scoped to USD and the native `SettledCash` tag. Its status is
`MISSING`, `INVALID`, `AMBIGUOUS`, `UNVERIFIED_SCOPE` or `OBSERVED`. Only a
single valid account-values-cache row with no model code includes the value,
source method, model scope and snapshot observation time. A `CashBalance` or
settlement schedule is not substituted for `SettledCash`; observation does not
prove spendable funds or freshness.

The receipt is absent when account facts cannot be built, including missing or
invalid cash rows, or when the physical-account selector does not match. Legacy
reports can also lack it. A consumer must treat an absent receipt as
`NOT_REPORTED` and not ready; absence does not establish why the evidence is
missing. This reporting state does not alter the separate execution gate.

[Chinese README](README.zh-CN.md)

InteractiveBrokersPlatform is QuantStrategyLab's Interactive Brokers multi-market execution runtime. It runs US and HK equity profiles through IBKR runtime services, under dry-run, paper, and live controls. It is an execution layer, not a strategy research repository: strategy logic comes from `UsEquityStrategies` / `HkEquityStrategies`, and snapshot or validation artifacts come from `UsEquitySnapshotPipelines` / `HkEquitySnapshotPipelines` when a profile requires them.

> Investing involves risk. This project does not provide investment advice and is for education, research, and engineering review only.

## Runtime boundary

- Loads only runtime-enabled strategy profiles exposed by the strategy packages.
- Handles broker/API connectivity, dry-run checks, notifications, and deployment settings.
- Must keep credentials in GitHub Secrets, cloud secret stores, or the broker-specific secret system, never in Git.
- Should start with dry-run or paper mode before any live order path is enabled.

## Direct vs snapshot-backed profiles

Direct runtime profiles can usually run from market history or portfolio state. Snapshot-backed profiles need a current artifact bundle from the matching snapshot pipeline before this platform should execute them. The platform should not invent strategy eligibility; it should consume the status and artifacts published by the strategy and snapshot repositories.

## Deploy safely

1. Configure secrets and runtime variables outside Git.
2. Run the workflow or service in dry-run mode.
3. Review generated orders, logs, notifications, and reconciliation output.
4. Confirm rollback steps and artifact versions.
5. Enable scheduled or live execution only after the above checks are clear.

## Repository layout

- `config/`: public non-sensitive runtime-target manifest.
- `tests/`: unit, contract, and regression tests.
- `docs/`: runbooks, design notes, evidence, and integration contracts.
- `.github/workflows/`: CI, scheduled jobs, release, or deployment workflows.
- `scripts/`: operator scripts and local helpers.
- `research/`: research configs and non-live candidate artifacts.

## QSL architecture role

- **Layer**: `runtime-platform`.
- **Responsibility**: Interactive Brokers multi-market execution runtime.
- **Owns**: IBKR connectivity, paper/live controls, account/runtime integration.
- **Consumes**: UsEquityStrategies, HkEquityStrategies, snapshot artifacts, QuantPlatformKit, QuantRuntimeSettings.
- **Must not**: own strategy research logic or bypass dry-run checks.

## Quick start

```bash
uv sync --frozen --extra test
uv run --no-sync ruff check --exclude external .
uv run --no-sync python scripts/check_qpk_pin_consistency.py
```

## Useful docs

- [`docs/hk-equity-runtime.md`](docs/hk-equity-runtime.md)
- [`docs/ibkr-paper-top50-switch.md`](docs/ibkr-paper-top50-switch.md)
- [`docs/ibkr-runtime-rollout.md`](docs/ibkr-runtime-rollout.md)
- [`docs/runtime-target-manifest.md`](docs/runtime-target-manifest.md): public runtime-target manifest contract, add/remove steps, and next-phase workflow wiring boundary

## Community and security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request scope, local verification, and documentation expectations.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for maintainer and contributor conduct.
- Report credential, automation, broker, exchange, or cloud-resource vulnerabilities through [SECURITY.md](SECURITY.md); do not open public issues for secrets or live-execution risk.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). AI assistants and maintainers follow [AGENTS.md](AGENTS.md).

## License

See [LICENSE](LICENSE).

Display/notification naming configuration: [display names](docs/display-names.md).
