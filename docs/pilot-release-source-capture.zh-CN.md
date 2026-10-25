# 单 TQQQ 现役配置来源的新观察快照

本入口保存本次观察到的实际 serving 来源。它不重建旧 release manifest、旧 evidence 或旧政策原文，不替换旧摘要；缺失旧材料继续是 `UNAVAILABLE_NOT_RECONSTRUCTED`。快照不是可加载的新 release，`release_qualified`、`recovery_authorized`、`execution_authorized` 始终为 false，不解除暂停或熔断。

在原仓库 main 首次运行 `Capture IBKR pilot release source observation`，选择唯一 TQQQ profile，传本次准确 main 的完整 SHA。沿用原部署 gate、受保护 inventory、认证来源和部署 concurrency，不复制真实账号、服务或 WIF 映射。准确 source checkout 必须匹配该 SHA 且干净；此 SHA 指采集代码，不声称它是现役镜像源码。

仅读取这一个 Cloud Run service 及实际唯一 100% serving revision。新 capture 的六项账户/部署身份、完整 compact release 必须与原 inventory 一致，镜像必须能解析为 immutable digest。保留当前 `RUNTIME_TARGET_JSON` 和 `QSL_RUNTIME_TARGET_JSON` 各自原始 UTF-8 字节：快照的 `observation.raw_target_json_by_env` 只含这两个固定名称，值以字符串封装，读回再 UTF-8 编码即可逐字节还原，未把重新序列化的对象冒充原文。沿用原 helper 的解析等价合同，允许空白等表示差异并保留各自原文；真正语义冲突、重复 JSON 键、NaN/Infinity、疑似凭据键或字符串均拒绝。只保存原 target、compact release、image/revision 和来源观察元数据，不保存全部 env、凭据、Secret 内容、token、券商数据或报告原文。revision 的 commit label 只作为观察值，不当成镜像源码证明。

目的地只从该 serving revision 的 `EXECUTION_REPORT_GCS_URI` 派生，追加固定 `release-source-snapshots/v1/<capture_run_id>-<bundle_sha256>.json`，仅支持 GCS。QPK provider 和实际 store 必须为 GCP，无本地回退，不列举或搜索对象。原 QPK `GcpObjectStore` 的 create/read 方法未开放重试参数，因此仅在本脚本的小函数内复用其同一个 client/blob，调用锁定 SDK 的 `upload_from_string(if_generation_match=0, retry=None, timeout=60)` 和 `download_as_bytes(retry=None, timeout=60)`。不新增 provider、依赖或 IAM。

每次只尝试一次 create-only 写入。409/412 仅对同一准确对象读回一次，字节完全相等才能识别已有快照；拒绝覆盖。权限拒绝即停；其他写错误报告 `write_unknown`，不重试、不推断没写入。写后或读回失败保留实际尝试/完成布尔，不声称未写。服务在初读、写前、读回后均需稳定；变化则停止，已写对象保留为当时观察，不改写。

完整 URI、revision 名和实际来源仅在受保护对象内保留。stdout 和唯一 artifact 仅含固定状态、受审 code SHA、run ID、布尔、固定 layout 版本、`capture_serving_revision_sha256` 及内容摘要。`bundle_sha256` 对完整 observation 寻址，`snapshot_sha256` 校验含来源元数据的完整 JSON 字节。

未来准确读回时，在同一 workflow 选择 `operation=readback`，仍传本次 main SHA，再填写原安全回执的 `capture_run_id`、`bundle_sha256`、`snapshot_sha256`。入口在原受保护环境中由报告 URI 和固定布局推导准确对象，只读一次；验证完整 snapshot hash、采集身份和当前服务稳定性，绝不 create/list/扫描。历史 compact release 与当前 release 的差异仅报告为布尔，不阻断因正常版本演进读取旧快照，也不授予恢复资格。

定位基准是 capture 时的原报告 URI。未来若其位置或物理账户目标迁移，须从原 capture serving revision 的受保护来源解析原 URI；本入口不会猜旧位置或搜索。当前原来源无法提供该位置、或对象不存在时明确 `source_locator_unavailable`，保留旧回执以供有权限的原来源核对；不新增可变 latest 索引或 registry。

没有 build/push/KEEP/deploy、环境配置、流量、Scheduler、IAM、Secret、券商、策略或通知操作。离线验证通过不代表云保存完成；只有本次准确读回及稳定性核对全部成功才报告 `saved`。
