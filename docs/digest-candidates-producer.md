English | [简体中文](digest-candidates-producer.zh-CN.md)

# IBKR DIGEST_CANDIDATES Producer

Repository: `InteractiveBrokersPlatform`  
Consumer: `QuantRuntimeSettings` (`daily_digest_aggregator` / Environment `runtime-strategy-switch`)

## Goal

Generate central daily-digest candidate JSON from **verified** IBKR `runtime_report.v1` reports (and optional account-facts history) for protected injection into QRS, without uploading public Actions artifacts by default or mutating QRS production Environments automatically.

Unlike Schwab, this repository has no `runtime-daily-sync` daily projection; its stable evidence feeds are:

1. Archived `runtime_report.v1` (strategy cycles and `execution_receipt`).
2. `ibkr_account_snapshot_history.v1` account-facts (equity projected by the heartbeat publisher).

## Candidate Contract

`platform_id` is fixed to **`ibkr`** (not `interactive_brokers`).

Minimal sanitized example using synthetic fixture values:

```json
{
  "schema_version": "qsl.digest_candidates.v1",
  "runs": [
    {
      "platform_id": "ibkr",
      "strategy_profile": "global_etf_rotation",
      "opaque_account_uid": "acct_opaque_example",
      "target_id": "ibkr/example-target",
      "account_hint": "U00000001",
      "account_scope": "live-u00000001",
      "actually_ran": true,
      "fill_count": null,
      "order_count": null,
      "cycle_count": 1,
      "field_status": {
        "fill_count": "counts_unknown",
        "order_count": "counts_unknown",
        "cycle_count": "known"
      },
      "evidence_provenance": "candidates",
      "reason_code": "ibkr_fills_not_projected",
      "signal_summary": "no_signal",
      "rebalance_kind": "no_rebalance",
      "status": "ok"
    }
  ],
  "producer_status": "projected",
  "producer_reason": ""
}
```

### Field Sources and Unknown Handling

| Field | Source | When unknown |
|---|---|---|
| `platform_id` | Fixed `ibkr` | — |
| `strategy_profile` | Report or `runtime_target.strategy_profile` | Documented fallback `global_etf_rotation` + `strategy_profile_defaulted` |
| `opaque_account_uid` | `IBKR_DIGEST_OPAQUE_ACCOUNT_UID` | Empty string + `opaque_account_uid_absent` |
| `target_id` | `IBKR_DIGEST_TARGET_ID` or `IBKR_ACCOUNT_FACTS_TARGET_ID` | Empty string + `target_id_absent` |
| `account_hint` | `IBKR_DIGEST_ACCOUNT_HINT` -> account-facts selector -> `runtime_target.account_selector` -> `RUNTIME_TARGET_JSON` -> `CLOUD_RUN_SERVICE_TARGETS_JSON` | Omit; central label falls back to scope/`live` |
| `account_scope` | `IBKR_DIGEST_ACCOUNT_SCOPE` -> `IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE` -> report/`RUNTIME_TARGET_JSON`/`CLOUD_RUN_SERVICE_TARGETS_JSON` | Omit |
| `actually_ran` | `true` only with a covering strategy cycle (`status`, `execution_receipt`, or `stage`) | Account-facts only without cycle evidence -> do not emit a run |
| `fill_count` / `order_count` | Never read from `summary.orders_*_count` | Must stay `null` + `counts_unknown` + `ibkr_fills_not_projected`; never turn quiet/dry-run `0` into verified zero |
| `cycle_count` | Covering reports for the same strategy and business day | — |
| `equity` | Optional account-facts `broker_reported_balances[].net_assets` (USD) | Omit field; never guess |
| `holdings` + `holdings_scope` | Runtime `summary.account_facts.broker_reported_positions` (STK only, `broker_reported_positions_scope=stocks_only`, optional `avg_cost`) passed through archive facts projection | Empty, malformed, abnormal, or negative short quantities -> omit both fields (never emit `[]`); stripped before console account-facts sync |
| `signal_summary` / `rebalance_*` | Mapped from `execution_receipt.outcome` or `status`/`stage` | Omit row if no cycle evidence exists |

## Local Projection (Synthetic or Existing JSON)

```bash
python3 scripts/project_digest_candidates.py \
  --runtime-report /path/to/runtime_report.v1.json \
  --account-facts /path/to/optional-account-facts.json \
  --opaque-account-uid 'acct_opaque_example' \
  --target-id 'ibkr/example-target' \
  --output /tmp/ibkr-digest-candidates.json
```

Stdout prints only a safe summary (`status` and `runs` count) without UIDs, targets, or equity amounts.

Unit tests (no QPK or `gcloud` dependency):

```bash
python3 -m pytest tests/test_project_digest_candidates.py tests/test_emit_digest_candidates.py -q
```

## Workflow (Optional, Off by Default)

`.github/workflows/execution-report-heartbeat.yml` provides `emit_digest_candidates` (default `false`). When enabled, a separate job:

- Reuses account-facts WIF, report prefix, and target mapping (read-only GCS access).
- Projects `project_ibkr_account_facts_history` read-only from the same runtime report to populate equity without POSTing.
- Writes to ephemeral `$RUNNER_TEMP/ibkr-digest-candidates.json` (and optional ephemeral facts).
- Optionally uploads a repo-private artifact (1-day retention) only when `capture_digest_candidates_artifact=true`.
- Never POSTs account-facts or modifies QRS Environments, logging only safe booleans (`equity_present`, `account_hint_present`).

Protected configuration (never committed to public repositories):

| Name | Purpose |
|---|---|
| `IBKR_DIGEST_OPAQUE_ACCOUNT_UID` | Opaque account identity |
| `IBKR_DIGEST_TARGET_ID` | Preferred target identifier; falls back to `IBKR_ACCOUNT_FACTS_TARGET_ID` |
| Existing account-facts secrets/vars | Shared with the heartbeat publisher to read reports |

Local or CI runs can also set `IBKR_DIGEST_RUNTIME_REPORT_PATH` to a local report file to skip GCS.

When selecting `additional-1`, `additional-2`, or `additional-3`, the CLI validates and loads the corresponding protected slot config before reading reports or writing output. Missing config returns `target_config_unavailable` and invalid or colliding config returns `target_config_invalid` (exit code `2`) without falling back to the primary slot. Valid slots still enforce account-facts identity, cross-account, and freshness checks.

`primary-live` and unset/empty targets preserve production defaults; unrecognized slots or ingress diagnostic actions exit `2` with `target_disabled` before reading reports. Offline wrong-slot regression tests do not override real runtime target mismatches: report identity must still be verified field by field against protected slot configs.

On identity mismatches, the publisher preserves first-line `skipped:runtime_target_mismatch` and exit code `1`, followed by a JSON line containing only `reason` and `mismatched_fields` restricted to `project_id`, `service_name`, `runtime_revision`, `account_scope`, `deployment_selector`, `account_selector`, and `runtime_release_receipt.runtime_revision`. It never prints expected or actual values, account IDs, service mappings, or report paths. `mismatched_fields` reports only the first failing check stage (deployment/account identity before receipt revision) rather than a full diff.

### Four-Slot Read-Only Identity Diagnostic

`.github/workflows/account-facts-identity-diagnose.yml` runs manually on `main` across `primary-live` and the three additional slots. It runs an offline config preflight before WIF, reads only the latest report under the protected prefix and Cloud Run metadata for the expected service, and never calls the publisher, Telegram, broker APIs, GCS writes, or artifact uploads.

`scripts/diagnose_account_facts_identity.py` outputs fixed reason codes, mismatched field names, guard/freshness status, boolean equalities across report/config/ready/receipt revisions, and the source SHA (`null` if missing). `identity_verified` reflects only the projection guard rather than release approval, installed dependencies, or digest recovery. Exit `0` requires a fresh report (36h past / 5m future bounds) and matching ready revision; mismatches or unavailable reads exit `1` with sanitized diagnostics.

Cloud Run queries require explicit `CLOUD_RUN_REGION` (`us-central1` in platform defaults) without inferring from reports or synthetic manifests. Report-vs-service equality never authorizes rotating expected revisions, and missing image/install/config/risk fingerprints remain `UNKNOWN`.

`serving_traffic` projects `status.traffic` read-only (`status=single|split|absent|invalid`) alongside nullable booleans `report_has_positive_traffic`, `configured_has_positive_traffic`, and `report_is_only_serving_revision`. Only valid integer percentages summing to `100` with explicit `revisionName` values are compared, summing duplicate lines per revision and excluding zero-traffic tags. Exit `0` never proves sole-serving status or approves identity rotation.

## Injecting into QRS (Manual)

See `QuantRuntimeSettings` `docs/digest-candidates-wiring.zh-CN.md` (and its English counterpart).

1. Generate candidate JSON from synthetic fixtures or an authorized `emit_digest_candidates` run.
2. Verify the JSON has no raw account numbers or tokens, and `fill_count` / `order_count` are `null` rather than `0`.
3. Have an authorized maintainer place the JSON into QRS Environment `runtime-strategy-switch` secret `DIGEST_CANDIDATES_JSON` (or pass a private file via `DIGEST_CANDIDATES_PATH`).
4. Dispatch `daily-digest-notify.yml` in QRS with `dry_run=true` and confirm `source_coverage.candidates_loaded=true` and unknown fills wording before considering a non-dry-run dispatch.

### Local Integration with the Central Aggregator

```bash
python3 scripts/project_digest_candidates.py \
  --runtime-report /tmp/synthetic-ibkr-report.json \
  --opaque-account-uid 'acct_opaque_example' \
  --target-id 'ibkr/example-target' \
  --output /tmp/ibkr-digest-candidates.json

python3 python/scripts/send_daily_digest_telegram.py \
  --dry-run --no-github \
  --business-day 2026-10-08 \
  --candidates /tmp/ibkr-digest-candidates.json \
  --write-receipt /tmp/digest-receipt.json
```

## Boundaries with Other Platforms

- Never modifies LB-HK, Firstrade sync, Cloud Run ingress, production strategies, or risk budgets.
- Emit paths never POST account-facts, refresh Flex, or edit Cloud Scheduler.
- Only covers IBKR candidate projection.

## Open Baseline Unknowns

- Whether production `IBKR_DIGEST_TARGET_ID` and opaque UID match console bindings: unknown (maintainer-supplied).
- Which `strategy_profile` a multi-strategy live account binds to: unknown (`global_etf_rotation` is a documented sole-target fallback only).
- When a dedicated fills ledger will exist for projection: unknown (`0` remains forbidden until then).
- Whether QRS allowlists still omit IBKR daily workflows: independent of this candidate pipeline.
