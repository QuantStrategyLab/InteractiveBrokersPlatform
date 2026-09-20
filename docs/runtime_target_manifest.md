# InteractiveBrokersPlatform runtime-target manifest

## 结论

`config/runtime_targets.manifest.json` 是 InteractiveBrokersPlatform 的公开、非敏感 runtime-target 清单。它用标准库 JSON 表达现有 4 个 live 目标与 1 个 `us_combo_shadow` 的必要字段，并提供严格校验。

本文件**不是**当前生产启停真相源。Cloud Run / GitHub Environment 变量（尤其 `RUNTIME_TARGET_ENABLED`）、Secret Manager 内容和实际部署保持不变；本批也没有把 workflow matrix 改成动态读取该 manifest。

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

## 现有 4 live + 1 shadow 示例

仓库内示例已表达当前 workflow / env-sync 使用的公开结构：

| id | service | account_group | execution_mode | include_lifecycle | include_reconciliation |
| --- | --- | --- | --- | --- | --- |
| `soxl_soxx_trend_income` | `interactive-brokers-quant-live-u15998061-service` | `live-u15998061` | `live` | true | true |
| `tqqq_growth_income` | `interactive-brokers-quant-live-u16608560-service` | `live-u16608560` | `live` | true | true |
| `global_etf_rotation` | `interactive-brokers-quant-live-u18308207-service` | `live-u18308207` | `live` | true | true |
| `russell_top50_leader_rotation` | `interactive-brokers-quant-live-u18336562-service` | `live-u18336562` | `live` | true | true |
| `us_combo_shadow` | `interactive-brokers-us-combo-shadow-service` | `us-combo-shadow` | `shadow` | false | false |

示例中五个目标的 `enabled` 均为 `false`。这表示公开清单的安全默认值，**不**覆盖 Environment / Cloud Run 里现有的启停状态，也不授权交易。

## 如何增减目标（本批之后的操作顺序）

新增目标：

1. 在 `config/runtime_targets.manifest.json` 增加一条目标；`enabled` 保持 `false`。
2. 为该目标准备受保护的 Environment / Secret Manager 名称引用；密钥值、Gateway 主机端口、client id、`account_ids` 与 continuity 指纹一律不进仓库。
3. 本地运行：

```bash
uv run --no-sync python scripts/validate_runtime_target_manifest.py
uv run --no-sync python -m pytest -q tests/test_runtime_target_manifest.py
```

4. 在**后续 wiring 批次**再考虑让 Guard / Lifecycle / Reconciliation / Deploy workflow 读取该清单；在那之前不要假设改 manifest 就会改变运行矩阵。

减少目标：

1. 先确认对应服务已停用、Scheduler / Cloud Run / reconciliation 不再需要该目标。
2. 从 manifest 删除该条目并保持校验通过。
3. Environment / Secret / Cloud Run 的实际清理另授权，不由本文件自动执行。

## 下一阶段 dynamic matrix 边界（明确未做）

本批完成：schema、示例、校验、parity tests、文档。

本批**不**做：

- 把 `runtime-guard.yml`、`runtime-target-lifecycle.yml`、`collect-reconciliation-evidence.yml`、`execution-report-heartbeat.yml`、`sync-cloud-run-env.yml` 的硬编码 matrix / 变量改成动态读取 manifest
- 修改任何 GitHub Secret / Environment 内容、生产开关、部署流程或交易逻辑
- 云端写入、交易、Scheduler pause/resume、流量切换

后续若要接线，建议最小边界：

1. 先让只读 workflow（Guard / Lifecycle / Heartbeat / Reconciliation）从 manifest 的 `include_*` 标志生成 matrix，但仍以 Environment / Cloud Run 的 `RUNTIME_TARGET_ENABLED` 为启停真相。
2. Deploy / env sync 再单独迁移；新建目标默认 `enabled=false`，不会自动部署或启用。
3. `include_reconciliation` 继续只允许 live；shadow 不得进入 reconciliation 矩阵。
4. 任何把 manifest `enabled` 提升为生产权威的改动，必须另开有授权的批次，并保留 fail-closed 读回。

## 本地校验

```bash
uv run --no-sync python scripts/validate_runtime_target_manifest.py
uv run --no-sync python scripts/validate_runtime_target_manifest.py --json
uv run --no-sync python -m pytest -q tests/test_runtime_target_manifest.py
```
