# InteractiveBrokersPlatform runtime-target manifest

## 结论

`config/runtime_targets.manifest.json` 是公开的**合成示例**，用标准库 JSON 展示 4 个 live 目标与 1 个 shadow 目标的字段契约；其中服务名和账户组编号均为虚构值。

本文件**不是**当前生产启停或账户映射真相源。生产目标由受保护的 `CLOUD_RUN_SERVICE_TARGETS_JSON` 配置，Cloud Run 的 `RUNTIME_TARGET_ENABLED`、Secret Manager 和实际调度状态须分别读回；`collect-reconciliation-evidence.yml` 从受保护配置选择对账目标，公开矩阵只包含策略 profile，不携带私有服务名。

## 字段契约

顶层：

| 字段 | 要求 |
| --- | --- |
| `schema_version` | 固定为 `1` |
| `platform_id` | 固定为 `ibkr` |
| `targets` | 至少一个目标对象 |

每个目标必填：

| 字段 | 含义 |
| --- | --- |
| `id` | 目标唯一 id（如 `soxl_soxx_trend_income` / `us_combo_shadow`） |
| `label` | 面向运维的短标签 |
| `service` | Cloud Run 服务名（全局唯一） |
| `region` | Cloud Run region |
| `account_group` | 公开账号组名（全局唯一；不是 `account_ids`） |
| `strategy_profile` | 预期策略 profile 名 |
| `execution_mode` | 仅允许 `live` / `paper` / `shadow` |
| `lifecycle_role` | 必须与 `execution_mode` 一致（live/shadow 语义） |
| `include_lifecycle` | 是否纳入 lifecycle 矩阵候选 |
| `include_reconciliation` | 是否纳入 reconciliation 矩阵候选；**仅 live 可为 true** |

可选：

- `enabled`：缺省为 `false`；新增目标必须按 disabled 起步
- `description`：短说明（不得嵌入密钥值）

校验还会拒绝：

- 重复的 `id` / `service` / `account_group`
- `account_ids`、Gateway 主机/端口/实例、`client_id` / `ib_client_id`、token、continuity 指纹、账户凭据等敏感字段
- 把长串 opaque 密钥值直接写进 manifest
- 未知顶层或目标字段

## 合成的 4 live + 1 shadow 示例

仓库内示例只表达字段结构，编号不对应真实账户或生产 Cloud Run 服务：

| id | service | account_group | execution_mode | include_lifecycle | include_reconciliation |
| --- | --- | --- | --- | --- | --- |
| `soxl_soxx_trend_income` | `ibkr-quant-live-demo01-service` | `live-u00000001` | `live` | true | true |
| `tqqq_growth_income` | `ibkr-quant-live-demo02-service` | `live-u00000002` | `live` | true | true |
| `global_etf_rotation` | `ibkr-quant-live-demo03-service` | `live-u00000003` | `live` | true | true |
| `russell_top50_leader_rotation` | `ibkr-quant-live-demo04-service` | `live-u00000004` | `live` | true | true |
| `us_combo_shadow` | `interactive-brokers-us-combo-shadow-service` | `us-combo-shadow` | `shadow` | false | false |

示例中五个目标的 `enabled` 均为 `false`。这表示公开清单的安全默认值，**不**覆盖 Environment / Cloud Run 里现有的启停状态，也不授权交易。

## 如何增减目标

新增目标：

1. 在受保护的 `CLOUD_RUN_SERVICE_TARGETS_JSON` 中增加目标，先保持 `RUNTIME_TARGET_ENABLED=false`；不要把真实账户编号、服务名或项目映射写进公开 manifest、测试与文档。
2. 为该目标准备受保护的 Environment / Secret Manager 引用；密钥值、Gateway 主机端口、client id、`account_ids` 与 continuity 指纹一律不进仓库。
3. 公开示例格式变更时，本地运行：

```bash
uv run --no-sync python scripts/validate_runtime_target_manifest.py
uv run --no-sync python -m pytest -q tests/test_runtime_target_manifest.py
```

4. 按目标核对部署清单、只读对账矩阵和实际 Cloud Run / Scheduler 状态；公开 manifest 的增减不会改变生产运行矩阵。

减少目标：

1. 先确认对应服务已停用、Scheduler / Cloud Run / reconciliation 不再需要该目标。
2. 从受保护配置移除该条目；公开示例无需与生产目标逐一对应。
3. Environment / Secret / Cloud Run 的实际清理另授权，不由本文件自动执行。

## 运行边界

公开 manifest 仅用于 schema、示例和离线校验。生产部署、每日演练与只读对账从受保护配置取目标，并以 Cloud Run / Scheduler 读回为运行事实。`include_reconciliation` 只用于 live 目标；shadow 不进入对账矩阵。新增目标默认停用，配置清单本身不授予交易权限。

## 本地校验

```bash
uv run --no-sync python scripts/validate_runtime_target_manifest.py
uv run --no-sync python scripts/validate_runtime_target_manifest.py --json
uv run --no-sync python -m pytest -q tests/test_runtime_target_manifest.py
```


The manual `Inspect Historical IBKR Execution` workflow resolves the actual live
service from protected inventory. Its optional execution-ledger summary also
aggregates UTC claim dates and signal/effective dates for claims without outcomes.
Unclassified or missing dates remain `null`. These are local execution records,
not a broker open-order count or authorization to clear a claim. The diagnostic
reads bounded objects without connecting to the broker and emits no account,
order, object URI, marker key or financial values.

## Short service names and staged migration

The four synthetic live examples use `ibkr-quant-live-demoNN-service`, with no
account identifier in the service name. Each is 30 characters; a 13-character
`cand-01234567` tag gives a combined length of 43, within the shared
`cloud-run-tags/v1` budget of 46. Production candidates use a private, unique
opaque slot instead of `demoNN`; resolve the exact old-to-new mapping from the
approved protected inventory. The examples are not that mapping. The shadow
example is outside this migration.

Set the same approved short name in every applicable protected inventory field:
`service`, `service_name` and `runtime_target.service_name`. The existing sync-plan
builder rejects a mismatch and runs the shared tag preflight. Do not add an
implicit long-to-short fallback: missing, duplicate, ambiguous or mismatched
bindings must stop preparation. Keep account selectors, strategy profiles,
execution modes and risk settings unchanged when preparing a candidate inventory.
The public manifest cannot change a deployed target or the protected inventory.

Cloud Run does not rename an existing service in place. A later approved operation
must create a replacement and explicitly migrate consumers. Before requesting
that operation, prepare and review the following against a fresh source readback:

1. Bind the exact old/new service pair, project, region and source revision; verify
   the destination is absent or belongs to this same approved migration. An
   unknown destination owner or service collision blocks the operation.
2. Revalidate the runtime-target fingerprint, execution claims, continuity and
   strategy-release bindings. A changed `service_name` changes target identity;
   carrying the old fingerprint, release admission or continuity receipt forward
   without revalidation is not valid. Do not reset, duplicate or fabricate order
   markers or opening state.
3. Inventory Scheduler URLs and OIDC audiences, health probes, reconciliation,
   account-facts consumers, notification producers, runtime-setting selectors and
   rollback references. Prepare coordinated updates so both services cannot
   execute the same account concurrently. Do not enable a Scheduler job or
   execution on the replacement as part of source preparation.
4. Obtain separate owner authorization bound to the destination and reviewed
   revision before any deployment, traffic, protected configuration, IAM, Secret
   or Scheduler mutation. Use the established guarded workflow and require its
   dry-run and before/after readbacks; this document adds no workflow or bypass.
5. Keep the existing service and its evidence until the approved migration has
   verified consumer adoption and rollback. An interrupted or uncertain migration
   requires reconciliation and a fresh readback before another attempt; never
   infer success from a short name or a green offline test.

The source-only short-name change leaves all five example targets disabled.
It does not deploy, change traffic, switch execution mode, approve continuity,
qualify PAPER sessions or enable live trading, Kelly or stop-loss controls.
