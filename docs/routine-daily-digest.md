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

## Grouping scheduled calls

`config/cloud-run-scheduled-dispatch.yaml` is shared by the digest candidate
group and the account-facts probe group. Deploy each group as a separate
Workflows resource with its own identity and one Scheduler job. Keep the same
cron, time zone and zero automatic retries. Set `DISPATCH_TARGETS` through
deployment configuration. Each entry has a short diagnostic `label` and either
`kind: job` with a fully qualified `job` resource, or `kind: service` with an
internal Cloud Run `audience` URL that serves `/probe`. Cloud Run targets keep
their own runtime identity and configuration.

Use an invoker binding on each exact target, and scope Scheduler's identity
to starting the corresponding workflow. Requests run in parallel. The
workflow records dispatch outcomes by label. For a batch job, dispatch
acceptance must be followed by a successful child execution and candidate
readback. For a probe, the HTTP result is followed by the normal account-facts
readback. Observe each group through its next planned window before retiring
the previous Schedulers. Restore the prior Scheduler definitions if rollback
is required. Never group live execution or broker session controls here.
