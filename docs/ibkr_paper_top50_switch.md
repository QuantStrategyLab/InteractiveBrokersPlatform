# IBKR Paper Top50 Balanced Switch Runbook

This branch is for a paper-account rehearsal only. Do not use it for live IBKR.

## Safety Gates

- `ACCOUNT_GROUP=default` must resolve to `ib_gateway_mode=paper`.
- `IBKR_PAPER_LIQUIDATE_ONLY=true` is guarded in code and raises if the selected account group is not paper.
- Keep `IBKR_DRY_RUN_ONLY=true` for the first invocation after every deploy or env change.

## Required Runtime Env

```text
STRATEGY_PROFILE=mega_cap_leader_rotation_top50_balanced
ACCOUNT_GROUP=default
IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME=ibkr-account-groups
IBKR_FEATURE_SNAPSHOT_PATH=gs://qsl-runtime-logs-interactivebrokersquant/strategy-artifacts/us_equity/mega_cap_leader_rotation_top50_balanced_staging/mega_cap_leader_rotation_top50_balanced_feature_snapshot_latest.csv
IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH=gs://qsl-runtime-logs-interactivebrokersquant/strategy-artifacts/us_equity/mega_cap_leader_rotation_top50_balanced_staging/mega_cap_leader_rotation_top50_balanced_feature_snapshot_latest.csv.manifest.json
NOTIFY_LANG=zh
```

## Rehearsal Sequence

1. Publish a fresh `mega_cap_leader_rotation_top50_balanced` snapshot/manifest/ranking/release summary to the GCS prefix above.
2. Deploy this branch to `interactive-brokers-quant-service`.
3. Set `IBKR_DRY_RUN_ONLY=true` and invoke once. Confirm the notification is Chinese and the target list is sensible.
4. Set `IBKR_PAPER_LIQUIDATE_ONLY=true` and keep `IBKR_DRY_RUN_ONLY=true`; invoke once to preview paper liquidation orders.
5. Remove `IBKR_DRY_RUN_ONLY`, keep `IBKR_PAPER_LIQUIDATE_ONLY=true`; invoke once to clear the paper account.
6. Remove `IBKR_PAPER_LIQUIDATE_ONLY`; invoke once to run the Top50 balanced strategy on the cleared paper account.
7. Review Telegram, Cloud Logging, and the execution report artifact before any future live discussion.

## Rollback

Restore the previous paper strategy env:

```text
STRATEGY_PROFILE=soxl_soxx_trend_income
unset IBKR_FEATURE_SNAPSHOT_PATH
unset IBKR_FEATURE_SNAPSHOT_MANIFEST_PATH
unset IBKR_PAPER_LIQUIDATE_ONLY
unset IBKR_DRY_RUN_ONLY
```
