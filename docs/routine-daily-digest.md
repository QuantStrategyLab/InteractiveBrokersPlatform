# Routine daily digest

Reuse the existing private candidates and central daily digest. The generic
Routine cycles, known broker acknowledgements and healthy heartbeats are
persisted for the daily digest regardless of legacy routing flags. Unknown
outcomes, reconciliation failures, risk and persistence errors remain immediate.
An acknowledged or partially filled order is not proof of a full fill.

Results and account reports still follow the existing persistence path.
Deferral is not delivery. This notification policy does not change risk
evaluation, trading settings, recipient mapping or schedules.

Validate fresh matching private candidates, window coverage, actual central
delivery and urgent-path verification. Keep unknown fill counts unknown. Existing templates support
`zh-CN` and `en`; configure language independently of routing.

中文：正常周期、已确认接收的订单和健康心跳进入日报；订单状态未知、对账失败、
风控和持久化异常即时通知。券商已接收或部分成交不代表完全成交。


## Scheduled account-facts revision

The natural heartbeat sets `IBKR_ACCOUNT_FACTS_RESOLVE_SERVING_REVISION=true`.
Its account-facts publisher reads the sole 100% serving revision for the protected
project, region and service, using the explicit existing workflow account. The
revision's runtime platform, service, account scope, account selector and deployment
selector must match the configured account identity. It rechecks that revision
after selecting the report and before publication. Split traffic, missing metadata,
identity drift or a revision change fail closed.

This replaces a stale expected-revision setting for the scheduled path only. The
report must still match the resolved revision and its release receipt; original
report timestamps, provenance and source-binding construction are unchanged. The
receiver must still authorize that exact source. A deployment does not enroll a
new receiver source. Named-report/manual recovery keeps the protected configured
revision; serving resolution rejects named reports. This code does not update
GitHub settings, receiver bindings or deployment state.
