# 实际承载版本的现金设置：只读诊断

`scripts.diagnose_account_cash_settings` 读取既有 Cloud Run 元数据，核对实际承载流量版本的现金配置声明。它不调用服务业务接口，不读取账户报告、券商、Secret 内容或容器镜像，不发布事实、修改配置或恢复熔断。

## 来源与边界

1. 由既有受保护 target 配置提供预期项目、服务、账户范围、账户选择和部署选择，并显式提供策略 profile 和区域。不从观测到的版本反推预期账户。
   函数入口沿用原账户选择序列合同，将合法 JSON 数组或元组规范为同一序列后精确比较；只统一表示形式，不解析别名或替换账户内容，也不修改调用方的预期绑定。
2. 读取服务元数据，通过原单一正流量版本检查选择 revision；latestReady、latestCreated 和零流量候选都不是回退来源。
3. 读取该精确 revision 的容器环境声明。须为单容器，并核策略、平台和原账户／服务范围；原 `RUNTIME_TARGET_JSON` 与 `QSL_RUNTIME_TARGET_JSON` 的合法形式均支持，两份显式声明不一致时拒绝，不擅自选择其中一份作为权威。
4. 再读一次服务，核实际正流量版本稳定。任一次读取失败、超时、身份不符或流量不确定，均不输出现金字段。单次元数据请求最长 30 秒，不自动重试。

声明来自 actual serving revision 仍不证明进程已经消费、已结算现金或风险政策完整。诊断始终保留 `runtime_consumption_verified=false`、`spendable_cash_verified=false`、`account_policy_bound=false`、`settings_applied=false` 和 `execution_authorized=false`。本入口与人工三档预览分开，真实声明不能改标为 synthetic 后送入离线计算。

## 原现金字段与覆盖关系

只检查原有四个字段：

- `IBKR_RESERVED_CASH_RATIO`、`QSL_IBKR_RESERVED_CASH_RATIO`。
- `IBKR_MIN_RESERVED_CASH_USD`、`QSL_IBKR_MIN_RESERVED_CASH_USD`。

比例和固定额均遵守原解析器的 QSL 覆盖优先级。空字符串可回退，只有空白的字符串先被选中后解析为空，不能混用。状态分别保留 `ABSENT`、`BLANK`、`EXPLICIT`、`SECRET_REFERENCE`、`INVALID`、`DUPLICATE`，显式零另用布尔标记。Secret 引用只表示存在引用，不解析或猜测其值；未配置不按零补齐，比例模式也不要求用户补造一个固定金额。

输出固定字段状态和所选字段名，不输出数值、原始环境、账户／服务标识、Secret 名称或异常文本。低熵数值的裸 SHA256 可被枚举，因此不输出金额或比例的数值哈希。调用方如需比较部署意图，可显式传入 `expected_declarations`，在内存中比较后只输出状态和数值是否相等；缺项或未读引用的比较保持未知。revision 哈希只用于记录观测身份，不是金融数值或策略资格。

## 使用原只读身份入口

在已经具有相应元数据读取权限的环境中，复用原 `IBKR_ACCOUNT_FACTS_*` target 配置和 `CLOUD_RUN_REGION`：

```bash
python -m scripts.diagnose_account_cash_settings --strategy-profile <所选策略profile>
```

原 target 解析仍校验已有受保护配置形状，但此诊断不列举或下载任何报告。函数入口 `diagnose_cash_settings` 也支持从既有已认证配置入口注入同一预期绑定，不要求公开或硬编码项目、服务或账户映射。CLI 输出 `OBSERVED` 的退出码为 0，表示元数据读取和范围核对完成；金融资格仍为 false。`UNAVAILABLE` 的退出码为 1，不能解释为默认值、空账户或线上故障。

原工作流未自动增加此步骤；本包不提交或触发远端流程，也不修改生产代码分派。

## 资金流和状态连续性仍需独立证据

现金环境声明不包含账户到账流水、盘中净值指数或可信初态。现有 IBKR Flex 原生 TWR 只绑定日期区间；不能据此宣称盘中外部资金流完整。QPK `cash_flow_adjusted_return` 只支持明确期末资金流约定，USDT 的日终区间合同也不能仅换币种标签接入 USD 盘中观察。

UES 已有账本保护高水位不下降、OPEN 不自动关闭；现有 journal 未保存上一已确认 adjusted index、相邻观察 NAV 和快照摘要，输入指纹也不能反推出这些值。缺少可信指数来源、前一指数锚和精确区间时继续留缺口，不临时拼造指数或另建账本。现金设置读回成功亦不等于这条连续性已经完成。
