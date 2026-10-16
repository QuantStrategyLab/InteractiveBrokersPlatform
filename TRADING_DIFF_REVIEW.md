# Trading diff review

这次修改只影响发布准入和流量切换。策略信号、仓位计算、风险阈值、订单提交、撤单和券商连接代码均未修改。

- `scripts/runtime_target_continuity.py` 复用当前固定版本 QPK 的 canonical hash 和 baseline assertion。只验证已有冻结基线，不生成或替换基线。
- `scripts/build_cloud_run_env_sync_plan.py` 校验 recovery ledger 与规范化处理后的最终目标；`verify_deployed_runtime_target_admission.py` 同样校验当前服务模板。
- protected 发布 workflow 的镜像和环境更新保留 `--no-traffic`，新增 `--deploy-health-check` 和 HTTP `GET /health` 启动探针。现有健康处理函数检查模块导入，不调用策略或订单入口。
- `reconcile_cloud_runtime.py` 在流量变更前读取选中的不可变 revision，检查名称、commit、冻结指纹、HTTP 探针以及 `Ready=True` / `ContainerHealthy=True`。检查失败即退出。
- 流量脚本改由 `uv run --no-sync` 使用 lockfile 环境，以便调用同一 QPK 检查。未新增依赖、云权限或外部健康请求。

正常目标沿用现有行为；错误或尚未证实启动成功的候选版本会阻止发布或切流。冻结指纹不匹配必须恢复一致配置，或另行批准完整基线迁移，不能靠重算 hash 放行。只有 TCP 探针的历史 revision 不能通过此 workflow 的新 promotion 检查；既有独立回滚流程未修改。dry-run 计划不读取或修改云资源。

验证：先补回归用例，旧检查出现 `DID NOT RAISE AdmissionError`；修改后 53 项相关 pytest 通过。改动文件 ruff、actionlint（关闭 shellcheck）和 `git diff --check` 通过。测试覆盖版本引用变化、保持 baseline 不变、合法 continuity 状态变化、HTTP/TCP 探针区别和失败候选不得调用 update-traffic。

首轮远端全量单元测试发现一个旧的 scheduler 用例只提供 `{"state": "RECONCILE_ONLY"}`，缺少完整 continuity baseline。已将该合成样本补齐为完整、匹配的 baseline，保留其暂停执行排程的原断言；生产检查没有放宽。修改后的 `tests/test_runtime_config_support.py` 全部 102 项本地测试通过，ruff 与完整 diff whitespace 检查通过。

另有已有检查失败：`bash tests/test_sync_cloud_run_env_workflow.sh` 仍期待硬编码 deploy service account，而 main 在 #637 已改为 GitHub variable。本次在未修改的 `origin/main` 临时快照上重跑，停在相同断言；未将此独立问题纳入修改。

本次没有部署、切流、Scheduler 调用、Secret/IAM 变更、券商请求或真实订单核对。上述验证证明本地发布保护逻辑，不能代替云上候选 revision 的实际启动验收。当前 incident 的归因和完整时间线见本任务交付的 REPORT.md。
