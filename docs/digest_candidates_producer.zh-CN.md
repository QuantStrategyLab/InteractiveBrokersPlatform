# IBKR DIGEST_CANDIDATES 生产者（P0-05）

审计日：2026-10-09  
仓库：InteractiveBrokersPlatform  
中央消费：QuantRuntimeSettings `daily_digest_aggregator` / Environment `runtime-strategy-switch`  
对照：CharlesSchwabPlatform [PR #488](https://github.com/QuantStrategyLab/CharlesSchwabPlatform/pull/488)

## 目标

从**已验证**的 IBKR `runtime_report.v1`（及可选 account-facts history）生成中央日报可消费的候选 JSON，经受保护通道注入 QRS，**不**默认上传公开 Actions artifact，**不**自动改 QRS 生产 Environment。

本仓**没有** Schwab 同款 `runtime-daily-sync` / daily projection；稳定证据线是：

1. 归档 `runtime_report.v1`（策略周期 / execution_receipt）
2. `ibkr_account_snapshot_history.v1` account-facts（权益；由既有 heartbeat publisher 投影）

## 候选合同（与 QRS #586 对齐）

`platform_id` 固定为 **`ibkr`**（不是 `interactive_brokers`）。

最小样例（脱敏；数字仅为合成夹具）：

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

### 字段来源与未知项

| 字段 | 来源 | 未知时 |
| --- | --- | --- |
| `platform_id` | 固定 `ibkr` | — |
| `strategy_profile` | report / `runtime_target.strategy_profile` | 回退文档化默认 `global_etf_rotation` + `strategy_profile_defaulted` |
| `opaque_account_uid` | `IBKR_DIGEST_OPAQUE_ACCOUNT_UID` | 空字符串 + `opaque_account_uid_absent` |
| `target_id` | `IBKR_DIGEST_TARGET_ID` 或 `IBKR_ACCOUNT_FACTS_TARGET_ID` | 空字符串 + `target_id_absent` |
| `account_hint` | `IBKR_DIGEST_ACCOUNT_HINT` → account-facts selector → report `runtime_target.account_selector` → `RUNTIME_TARGET_JSON` → `CLOUD_RUN_SERVICE_TARGETS_JSON` | 省略；中央标签会退化为 scope/`live` |
| `account_scope` | `IBKR_DIGEST_ACCOUNT_SCOPE` → `IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE` → report/`RUNTIME_TARGET_JSON`/`CLOUD_RUN_SERVICE_TARGETS_JSON` | 省略 |
| `actually_ran` | 有 covering 策略周期（status / execution_receipt / stage）才为 `true` | 仅 account-facts、无周期证据 → 不产出 run |
| `fill_count` / `order_count` | **不**读取 `summary.orders_*_count` | **必须** `null` + `counts_unknown` + `ibkr_fills_not_projected`；禁止把 quiet/dry-run 的 0 写成已验证零 |
| `cycle_count` | 同策略同业务日 covering report 数 | — |
| `equity` | 可选 account-facts `broker_reported_balances[].net_assets`（USD） | 省略字段；不猜 |
| `holdings` | 当前投影**无**持仓明细 | 省略；不猜 |
| `signal_summary` / `rebalance_*` | `execution_receipt.outcome` 或 status/stage 映射 | 无周期证据则整行不产出 |

## 本机投影（合成 / 已有 JSON）

```bash
python3 scripts/project_digest_candidates.py \
  --runtime-report /path/to/runtime_report.v1.json \
  --account-facts /path/to/optional-account-facts.json \
  --opaque-account-uid 'acct_opaque_example' \
  --target-id 'ibkr/example-target' \
  --output /tmp/ibkr-digest-candidates.json
```

stdout 只打印安全摘要（`status` / `runs` 计数），不含 uid、target、权益金额。

单测（**不依赖 QPK / gcloud**）：

```bash
python3 -m pytest tests/test_project_digest_candidates.py tests/test_emit_digest_candidates.py -q
```

## Workflow（可选，默认关）

`.github/workflows/execution-report-heartbeat.yml` 增加输入 `emit_digest_candidates`（默认 `false`）。

开启后单独 job：

- 复用 account-facts 的 WIF / report prefix / target 映射（只读 GCS report）
- 从同一 runtime report **只读**投影 `project_ibkr_account_facts_history` 填权益（**不** POST）
- 写出到 `$RUNNER_TEMP/ibkr-digest-candidates.json`（ephemeral）；可选写出 ephemeral facts
- 可选输入 `capture_digest_candidates_artifact=true` 上传 **repo-private** artifact（retention 1 day）供授权注入 QRS；默认关
- **不** POST account-facts / 不改 QRS Environment
- stdout 仅安全摘要（`equity_present` / `account_hint_present` 布尔，无金额、无 uid）

所需受保护配置（不得写入公开仓）：

| 名称 | 用途 |
| --- | --- |
| `IBKR_DIGEST_OPAQUE_ACCOUNT_UID` | 不透明账户身份 |
| `IBKR_DIGEST_TARGET_ID` | 优先；否则回退 `IBKR_ACCOUNT_FACTS_TARGET_ID` |
| 既有 account-facts secrets/vars | 与 heartbeat publisher 相同（读报告用） |

本地 / CI 也可只设 `IBKR_DIGEST_RUNTIME_REPORT_PATH` 指向已落盘报告，完全跳过 GCS。

## 如何注入 QRS（人工，不自动改生产 Environment）

中央文档：QuantRuntimeSettings `docs/digest-candidates-wiring.zh-CN.md`（PR #586）。

推荐步骤：

1. 在本仓用合成 fixtures 或获准的手动 `emit_digest_candidates` 得到候选 JSON。
2. 确认 JSON **无**原始账户号、无 token；`fill_count`/`order_count` 为 `null` 而非 `0`。
3. 由有权限的维护者把整份 JSON 写入 QRS 仓库 Environment **`runtime-strategy-switch`** 的 secret **`DIGEST_CANDIDATES_JSON`**（或私有前置步骤写出文件后设 `DIGEST_CANDIDATES_PATH`）。
4. 在 QRS 对 `daily-digest-notify.yml` 做 `workflow_dispatch`（默认 `dry_run=true`），检查 receipt：
   - `source_coverage.candidates_loaded=true`
   - IBKR run 的 fills provenance 非「已验证零」
   - 文案成交为「未知」而非「无成交」
5. 确认 dry-run 后再考虑关闭 dry-run；**本 IBKR PR 不会自动写入 QRS Environment**。

也可将 ephemeral 文件同步到私有 GCS，再由受保护 job 注入；同样禁止默认公开 artifact。

### 本机联调（与中央 aggregator）

```bash
# 1) 合成候选
python3 scripts/project_digest_candidates.py \
  --runtime-report /tmp/synthetic-ibkr-report.json \
  --opaque-account-uid 'acct_opaque_example' \
  --target-id 'ibkr/example-target' \
  --output /tmp/ibkr-digest-candidates.json

# 2) 在 QuantRuntimeSettings（#586 分支或已合并）
python3 python/scripts/send_daily_digest_telegram.py \
  --dry-run --no-github \
  --business-day 2026-10-08 \
  --candidates /tmp/ibkr-digest-candidates.json \
  --write-receipt /tmp/digest-receipt.json
```

## 与 quant / 其他平台边界

- 不改 LB-HK、Firstrade sync、Cloud Run ingress、生产策略或风险预算。
- emit 路径不 POST account-facts、不刷新 Flex、不改 scheduler。
- Schwab 生产者见 CharlesSchwabPlatform PR #488；本任务只扩 IBKR。

## 未知项（基线）

- 生产 `IBKR_DIGEST_TARGET_ID` / opaque uid 是否已与控制台 binding 一致：未知（待维护者填）
- 真实账户应绑定哪个 `strategy_profile`（多策略并存）：未知；缺省回退 `global_etf_rotation` 仅作文档化 sole-target 占位
- fills 何时有专用 ledger 可投影：未知；升级前禁止写 0
- QRS allowlist 对 IBKR daily workflow 是否仍 omitted：与候选管道独立；候选注入不依赖 GitHub stub
