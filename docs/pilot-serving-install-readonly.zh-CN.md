# 单 TQQQ serving 镜像安装身份只读核验

在原仓库 main 首次运行 `Read IBKR pilot serving install identity`，指定唯一 TQQQ profile 及本次 main 的完整 SHA。沿用既有部署 gate、原 inventory/认证来源和部署 concurrency；不扫描其他 slot，不增加 IAM，也不修改版本声明。

受保护 runner 读取准确唯一 100% serving revision，核对六项账户/部署身份，再按 revision 的确切 immutable digest 拉取镜像。Docker 认证只放 runner 临时目录。沿用原安装身份探针：`-I -S`、无网络、只读、非 root、drop capabilities、禁止提权，无主机卷或运行环境注入，不导入业务包。读取完成后再次确认 serving、流量/tags、账户身份、target 和 digest 稳定。

公开回执只含固定状态、每个包的 metadata 状态、nullable equality 和独立 `ues_identity_verified`。actual serving release 来自真实 serving target；`deployment_inventory_release` 来自受保护部署 inventory；source lock 只来自本次准确源码的 uv.lock，不能当成已批准风险版本。原版本值、镜像地址、服务、账号、绑定、配置和异常不输出或保存。

plugin 缺失或缺少提交 metadata 时整体保留 partial，退出码仍为 1；只要 UES distribution 的提交与 stamp 一致，独立 UES 身份仍可核实。stamp 与 distribution 冲突则明确不可用，不把冲突升级为策略版本合格。读取失败或身份漂移保留 unknown/null，不用零值或主分支版本补造。

此入口没有 build、push、KEEP、deploy、IAM、Secret 内容、券商、策略或通知操作。镜像安装身份取证不授予交易、风险政策、恢复或发布权限。
