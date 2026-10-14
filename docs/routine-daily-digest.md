# Routine daily digest migration (owner approval required)

Reuse the existing private candidates and central daily digest. The generic
`QSL_ROUTINE_NOTIFICATION_MODE` defaults to `immediate`; explicitly selecting
`daily_digest` defers verified healthy no-order notices from both normal and
no-op notification paths. Risk blocks, unknown/pending orders, reconciliation
or persistence errors and all real order activity remain immediate.

Results and account reports still follow the existing persistence path.
Deferral is not delivery. The QPK pin contains only notification changes on
the exact former consumer pin; this draft does not change risk evaluation,
trading settings, recipient mapping, schedules or production flags.

Approve deployment and a one-platform flag change only after fresh matching
private candidates, window coverage, actual central delivery and urgent-path
verification. Keep unknown fill counts unknown. Existing templates support
`zh-CN` and `en`; configure language independently of routing.

中文：默认保留现有通知。核验日报替代链后，负责人可批准正常无单周期进入同一份
日报；风控、订单未知、待对账和运行异常不等待日报。部署及开关变更须另行批准。
