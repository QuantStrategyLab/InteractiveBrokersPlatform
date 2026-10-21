"""IBKR notification integration; no broker, allocation, or risk decisions."""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import re
import json
import os

from notifications.digest_snapshot.unified_contract import (
    CandidatePersistenceError, IncidentTracker, NotificationEvent,
    effective_notification_mode, route_notification_event, unified_notification_enabled,
)
from notifications.routine_observations import account_alias as display_account_alias, routine_decisions

FLAG = "IBKR_UNIFIED_NOTIFICATION_ENABLED"
_RUN = ContextVar("ibkr_notification_run", default=None)


def enabled(env=None):
    values = os.environ if env is None else env
    return unified_notification_enabled(values, FLAG)


def effective_mode(env=None):
    values = os.environ if env is None else env
    return effective_notification_mode(values, FLAG)


def with_run(report, function, *args, **kwargs):
    if not enabled():
        return function(*args, **kwargs)
    token = _RUN.set(report)
    try:
        return function(*args, **kwargs)
    finally:
        _RUN.reset(token)


class GCSMarkerStore:
    """Generation-checked private notification state, separate from trading state."""
    def __init__(self, prefix, *, client=None):
        if not str(prefix).startswith("gs://"):
            raise ValueError("notification_marker_store_missing")
        from google.cloud import storage
        self.prefix = prefix.rstrip("/")
        self.client = client or storage.Client()
        self.blob_factory = storage.Blob.from_string

    def read(self, key):
        from google.api_core.exceptions import NotFound
        blob = self.blob_factory(f"{self.prefix}/{key}.json", client=self.client)
        try:
            body = blob.download_as_bytes(retry=None, timeout=30)
        except NotFound:
            return None, None
        if blob.generation is None:
            raise ValueError("notification_marker_generation_missing")
        return int(blob.generation), json.loads(body)

    def compare_and_set(self, key, generation, value):
        from google.api_core.exceptions import PreconditionFailed
        blob = self.blob_factory(f"{self.prefix}/{key}.json", client=self.client)
        try:
            blob.upload_from_string(json.dumps(value, allow_nan=False), content_type="application/json",
                                    if_generation_match=generation or 0, retry=None, timeout=30)
        except PreconditionFailed:
            return False
        return True


class GCSCandidateStore(GCSMarkerStore):
    def publish(self, key, revision, payload):
        """Publish schema-compatible JSON; generations prevent stale overwrites."""
        from google.api_core.exceptions import NotFound, PreconditionFailed
        identity_keys = ("platform_id", "opaque_account_uid", "target_id", "strategy_profile", "business_day")
        identity = [payload["runs"][0].get(k) for k in identity_keys]
        scope = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        for _ in range(8):
            blob = self.blob_factory(f"{self.prefix}/{key}.json", client=self.client)
            try:
                blob.reload(retry=None, timeout=30)
                generation = int(blob.generation)
                metadata = blob.metadata or {}
                if metadata.get("notification_scope") != scope:
                    old = json.loads(blob.download_as_bytes(retry=None, timeout=30))
                    if not old.get("runs") or any([row.get(k) for k in identity_keys] != identity for row in old["runs"]):
                        raise ValueError("notification_candidate_slot_identity_conflict")
                previous = int(metadata.get("notification_revision", "0"))
                if previous >= revision:
                    return True
            except NotFound:
                generation = 0
            blob.metadata = {"notification_revision": str(revision), "notification_scope": scope}
            try:
                blob.upload_from_string(json.dumps(payload, allow_nan=False), content_type="application/json",
                                        if_generation_match=generation, retry=None, timeout=30)
            except PreconditionFailed:
                continue  # A rejected CAS is known not to have written anything.
            return True
        raise RuntimeError("notification_candidate_concurrent_update")


def _state_store(env=None):
    values = os.environ if env is None else env
    return GCSMarkerStore(values.get("IBKR_NOTIFICATION_MARKER_GCS_URI", ""))


def persist_candidate(fact, *, actually_ran=True, dry_run=False, state_store=None, candidate_store=None, env=None,
                      block_reason=None, display_alias="", decisions=()):
    """Persist event/run identity and bridge the existing candidate producer."""
    from scripts.project_digest_candidates import project_notification_events
    if actually_ran is not True:
        raise CandidatePersistenceError("notification_candidate_business_run_required")
    if fact.get("coverage_status") != "known" or not fact.get("dedupe_key"):
        raise CandidatePersistenceError("notification_candidate_identity_unknown")
    values = os.environ if env is None else env
    slot = values.get("DIGEST_STORE_TARGET", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", slot):
        raise CandidatePersistenceError("notification_candidate_slot_missing")
    state_store = state_store or _state_store(values)
    candidate_store = candidate_store or GCSCandidateStore(values.get("DIGEST_CANDIDATES_GCS_URI", ""))
    parts = [fact[k] for k in ("platform_id", "opaque_account_uid", "target_id", "strategy_profile", "business_day")]
    key = "candidate-state/" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()
    record = {"event": fact, "actually_ran": actually_ran, "dry_run": dry_run}
    if block_reason:
        record["block_reason"] = block_reason
    alias = display_account_alias({"DIGEST_ACCOUNT_ALIAS": display_alias})
    observations = routine_decisions({"notification_routine_decisions": decisions})
    if alias:
        record["account_alias"] = alias
    if observations:
        record["routine_decisions"] = observations
    for _ in range(8):
        version, old = state_store.read(key)
        old = old or {"revision": 0, "events": {}}
        events = dict(old["events"])
        prior = events.get(fact["dedupe_key"])
        if prior is not None:
            incoming = record
            # Observation time may change across monitors; never replace facts
            # from the same run or lose an earlier abnormal observation.
            count_fields = ("order_count", "fill_count")
            def comparable(v):
                return {k: x for k, x in v["event"].items() if k not in ("occurred_at", *count_fields)}
            if comparable(prior) != comparable(record) or prior.get("dry_run") != dry_run:
                record = {**prior, "conflict": True}
            else:
                merged = dict(prior["event"])
                conflict = prior.get("conflict") is True
                for field in count_fields:
                    before, after = merged.get(field), fact.get(field)
                    conflict |= before is not None and after is not None and before != after
                    if before is None:
                        merged[field] = after
                record = {**prior, "event": merged,
                          **({"conflict": True} if conflict else {}),
                          **({"block_reason": block_reason} if block_reason else {})}
            # Display conflicts hide only the disputed observation. They never
            # manufacture an execution/count conflict or change the event kind.
            display_conflicts = set(prior.get("_display_conflicts", ())) & {"account_alias", "routine_decisions"}
            for name in ("account_alias", "routine_decisions"):
                if incoming.get(name):
                    if prior.get(name) and prior[name] != incoming[name]:
                        display_conflicts.add(name)
                    elif name not in display_conflicts:
                        record[name] = incoming[name]
            for name in display_conflicts:
                record.pop(name, None)
            if display_conflicts:
                record["_display_conflicts"] = sorted(display_conflicts)
        events[fact["dedupe_key"]] = record
        changed = events != old["events"]
        value = {"revision": old["revision"] + int(changed), "events": events}
        if not changed or state_store.compare_and_set(key, version, value):
            payload = project_notification_events(list(events.values()))
            object_key = f"{fact['platform_id']}/{fact['business_day']}/{slot}"
            return candidate_store.publish(object_key, value["revision"], payload) is True
    raise RuntimeError("notification_candidate_concurrent_update")


def _identity(target=None):
    report = _RUN.get() or {}
    configured = json.loads(os.environ.get("RUNTIME_TARGET_JSON") or "{}")
    runtime = target or report.get("runtime_target") or configured
    values = dict(os.environ)
    bindings = json.loads(values.get("DIGEST_TARGET_BINDINGS_JSON") or "[]")
    if target is not None:
        def matches(bound):
            required = ("service_name", "strategy_profile", "account_scope")
            optional = ("account_selector", "deployment_selector", "dry_run_only")
            return isinstance(bound, Mapping) and all(bound.get(k) == runtime.get(k) and runtime.get(k) for k in required) and all(
                bound.get(k) == runtime[k] for k in optional if k in runtime)
        matched = []
        for binding in bindings:
            bound = binding.get("RUNTIME_TARGET_JSON")
            bound = json.loads(bound) if isinstance(bound, str) else bound
            if matches(bound):
                matched.append(binding)
        if len(matched) == 1:
            values.update(matched[0])
            if "DIGEST_ACCOUNT_ALIAS" not in matched[0]:
                values.pop("DIGEST_ACCOUNT_ALIAS", None)
        elif matched or not matches(configured):
            for key in ("DIGEST_OPAQUE_ACCOUNT_UID", "DIGEST_TARGET_ID", "IBKR_DIGEST_OPAQUE_ACCOUNT_UID", "IBKR_DIGEST_TARGET_ID", "DIGEST_ACCOUNT_ALIAS"):
                values.pop(key, None)
    return {
        "platform_id": "ibkr", "opaque_account_uid": values.get("DIGEST_OPAQUE_ACCOUNT_UID") or values.get("IBKR_DIGEST_OPAQUE_ACCOUNT_UID"),
        "target_id": values.get("DIGEST_TARGET_ID") or values.get("IBKR_DIGEST_TARGET_ID"),
        "strategy_profile": runtime.get("strategy_profile") or report.get("strategy_profile") or values.get("STRATEGY_PROFILE"),
    }, values


def event_for(kind, *, reason=None, report=None, target=None, order_count=None, fill_count=None, now=None):
    source = report if report is not None else (_RUN.get() or {})
    stamp = source.get("started_at")
    stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else stamp
    identity, _ = _identity(target or source.get("runtime_target"))
    return NotificationEvent(
        event_kind=kind, occurred_at=stamp or now or datetime.now(timezone.utc), **identity,
        source_run_id=source.get("run_id"), incident_reason=reason,
        order_count=order_count, fill_count=fill_count,
    )


class OrderedMarkerStore:
    """Reject late observations rather than falsely establishing recovery."""
    def __init__(self, store, observed_at):
        self.store, self.observed_at = store, observed_at.isoformat()
    def read(self, key):
        version, value = self.store.read(key)
        if value and value.get("observed_at") and datetime.fromisoformat(value["observed_at"]) > datetime.fromisoformat(self.observed_at):
            raise RuntimeError("notification_incident_observation_stale")
        return version, value
    def compare_and_set(self, key, version, value):
        return self.store.compare_and_set(key, version, {**value, "observed_at": self.observed_at})


def _independent_sender_receipt(receipt, decision, event):
    """Keep opaque provider IDs; local reservation/event IDs are not receipts."""
    if not isinstance(receipt, str) or not receipt.strip():
        return None
    normalized = receipt.strip().casefold()
    namespaces = normalized.replace("/", ":").split(":")
    if any(part.strip().replace("_", "-") == "transport-ack" for part in namespaces):
        return None
    local_ids = (decision.incident_key, decision.transition_key,
                 event.to_candidate()["dedupe_key"], event.order_execution_digest)
    # Substring comparison also rejects namespaced IDs and marker object URIs.
    if any(value and value.casefold() in normalized for value in local_ids):
        return None
    return receipt


def immediate_incident(event, send, *, recovered=False, severity=1, store=None):
    tracker = IncidentTracker(OrderedMarkerStore(store or _state_store(), event.occurred_at))
    decision = tracker.observe(event, state="recovered" if recovered else event.event_kind,
                               severity=severity, recovered=recovered)
    if decision.action in {"duplicate", "recovered"}:
        # QPK deliberately skips an unchanged value. Advance the observation
        # cursor even for duplicates so a late recovery cannot reopen a claim.
        for _ in range(8):
            version, previous = tracker.store.read(decision.incident_key)
            if previous.get("observed_at") == event.occurred_at.isoformat() or tracker.store.compare_and_set(
                    decision.incident_key, version, previous):
                return True
        raise RuntimeError("notification_marker_concurrent_update")
    if decision.action != "send":
        raise RuntimeError("notification_incident_identity_unknown")
    try:
        outcome = send()
        acknowledged = outcome is True or isinstance(outcome, Mapping) and outcome.get("acknowledged") is True
    except Exception:
        tracker.record_delivery(decision)
        raise
    # A legacy boolean ACK is not a unique transport receipt. Keep it uncertain
    # rather than inventing a message ID; either state prohibits blind resend.
    receipt = outcome.get("receipt_id") if acknowledged and isinstance(outcome, Mapping) else None
    tracker.record_delivery(decision, receipt_id=_independent_sender_receipt(receipt, decision, event))
    return acknowledged


def _incident_index(event, family, store, *, add=None):
    scope = [event.platform_id, event.target_id, event.opaque_account_uid, event.strategy_profile, family]
    if not all(isinstance(v, str) and v.strip() for v in scope):
        raise ValueError("notification_incident_identity_unknown")
    key = "incident-index/" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()
    for _ in range(8):
        version, value = store.read(key)
        value = value or {"reasons": []}
        if add is None:
            return value["reasons"]
        reasons = sorted(set(value["reasons"]) | {add})
        if reasons == value["reasons"] or store.compare_and_set(key, version, {"reasons": reasons}):
            return reasons
    raise RuntimeError("notification_incident_index_concurrent_update")


def recover_incidents(event, *, family, store=None):
    from dataclasses import replace
    store = store or _state_store()
    for reason in _incident_index(event, family, store):
        immediate_incident(replace(event, incident_reason=reason), lambda: False, recovered=True, store=store)


def send_incident(event, send, *, family, severity=1, store=None):
    store = store or _state_store()
    _incident_index(event, family, store, add=event.incident_reason)
    return immediate_incident(event, send, severity=severity, store=store)


def _kind(execution, *, dry_run=False):
    if execution.get("auth_expired") or execution.get("auth_failure"):
        return "auth_failure"
    if any(execution.get(k) for k in ("error", "errors", "fail_reason", "plugin_error", "persistence_error",
                                     "report_persistence_error", "notification_error", "strategy_plugin_error")):
        return "real_error"
    if any(execution.get(k) for k in ("orders_pending", "option_orders_pending", "orders_partially_filled",
                                     "option_orders_partially_filled", "orders_pending_count", "option_orders_pending_count",
                                     "pending_reconciliation", "reconciliation_required")):
        return "order_status_unknown"
    status = str(execution.get("execution_status") or "").lower()
    if status in {"pending", "unknown", "pending_reconciliation", "submitted", "broker_acknowledged", "partially_filled"}:
        return "order_status_unknown"
    if status in {"error", "failed", "failure"}:
        return "real_error"
    if (status == "blocked" or status.endswith("_blocked")
            or any(execution.get(k) for k in ("execution_blocked_reason", "notification_attention_reason_codes",
                                              "account_new_risk_buy_blocked", "small_account_buy_blocked", "risk_flags"))
            or str(execution.get("risk_gate") or "").upper() in {"BLOCK", "BLOCKED", "REJECT", "REJECTED", "DENY", "DENIED"}):
        return "blocked_run"
    reason = str(execution.get("no_op_reason") or "").split(":", 1)[0]
    known_reasons = {"", "no_action", "no_signal", "no_rebalance", "below_threshold", "threshold_not_met",
        "rebalance_diff_below_threshold", "target_diff_below_threshold", "min_notional", "quantity_zero",
        "outside_execution_window", "outside_monthly_execution_window", "execution_already_recorded"}
    if reason not in known_reasons or status not in {"no_action", "no_op", "completed", "executed", "dry_run", "dry_run_completed"}:
        return "real_error"
    # Reuse every existing urgent-field exclusion. This copy is notification
    # evidence only; the execution result and its admission decision are intact.
    from notifications.digest_snapshot.digest_routing import defer_routine_cycle_to_digest
    guard = {**execution, "execution_status": "no_action", "no_op_reason": "", "broker_submission_done": False}
    if not defer_routine_cycle_to_digest(guard, submitted_order_count=0, mode="daily_digest"):
        return "real_error"
    orders = list(execution.get("orders_submitted") or ()) + list(execution.get("option_orders_submitted") or ())
    if not dry_run and orders:
        if any(not isinstance(order, Mapping) or str(order.get("status") or "").lower() not in {"filled", "completed", "executed"} for order in orders):
            return "order_status_unknown"
        return "order_submitted"
    if not dry_run and (execution.get("orders_filled") or execution.get("option_orders_filled")):
        return "fill"
    if not dry_run and (execution.get("broker_submission_done") or execution.get("orders_submitted_count")):
        return "order_status_unknown"
    return "routine_cycle"


def publish_cycle(publisher, *, notification, execution, attention=False):
    if not enabled():
        return publisher.publish(notification)
    from dataclasses import replace
    from notifications.digest_snapshot.digest_candidates import summary_evidence
    from notifications.digest_snapshot.unified_contract import order_identity_digest
    report = _RUN.get() or {}
    dry_run = report.get("dry_run") is True
    kind = _kind(execution, dry_run=dry_run)
    if attention and kind in {"routine_cycle", "order_submitted", "fill"}:
        kind = "receipt_unacceptable"  # Earlier attention cannot be cleared by merging a later summary.
    facts = summary_evidence(execution, dry_run=dry_run)
    reason = kind
    reason_code = str(execution.get("no_op_reason") or "").split(":", 1)[0]
    if re.fullmatch(r"[a-zA-Z0-9_]{1,100}", reason_code):
        reason += ":" + reason_code
    event = event_for(kind, reason=reason, order_count=facts["order_count"], fill_count=facts["fill_count"])
    if kind in {"order_submitted", "fill"}:
        orders = list(execution.get("orders_submitted") or ()) + list(execution.get("option_orders_submitted") or ())
        ids = [next((str(order[k]) for k in ("permId", "orderId", "order_id", "execution_id") if order.get(k) is not None), "") for order in orders]
        if ids and all(ids):
            event = replace(event, order_execution_digest=order_identity_digest(json.dumps(sorted(ids))))
    def immediate():
        if kind in {"order_submitted", "fill"}:
            return publisher.publish(notification)  # Real orders/fills retain their immediate sender.
        return send_incident(event, lambda: publisher.publish(notification), family="cycle")
    _, display_values = _identity(report.get("runtime_target"))
    try:
        result = route_notification_event(
            event, env=os.environ, flag_key=FLAG,
            persist_candidate=lambda fact: _checked_candidate(fact, actually_ran=True, dry_run=dry_run,
                display_alias=display_account_alias(display_values), decisions=routine_decisions(execution)),
            legacy=lambda: publisher.publish(notification), immediate=immediate,
        )
    except CandidatePersistenceError:
        from notifications.events import RenderedNotification
        zh = str(os.environ.get("QSL_NOTIFY_LANG") or os.environ.get("NOTIFY_LANG") or "zh").startswith("zh")
        notice = "⚠️ 汇总候选写入未确认；本次保留即时通知。" if zh else "⚠️ Digest candidate write unconfirmed; keeping this notification immediate."
        failure = replace(event, event_kind="real_error", incident_reason="candidate_persistence")
        try:
            return send_incident(failure, lambda: publisher.publish(RenderedNotification(
                notification.detailed_text + "\n" + notice, notification.compact_text + "\n" + notice)), family="candidate")
        except Exception:
            print("notification_candidate_and_marker_unavailable", flush=True)
            return False
    except Exception:
        print("notification_incident_marker_unavailable", flush=True)
        return False
    if not result.candidate_persisted:
        print("notification_candidate_write_unconfirmed", flush=True)
    else:
        try:
            recover_incidents(event, family="candidate")
        except Exception:
            print("notification_recovery_marker_unavailable", flush=True)
    if kind == "routine_cycle":
        try:
            recover_incidents(event, family="cycle")
            recover_incidents(event, family="runtime")
        except Exception:
            print("notification_recovery_marker_unavailable", flush=True)
    return result.result is not False


def _checked_candidate(fact, **kwargs):
    try:
        return persist_candidate(fact, **kwargs)
    except Exception:
        raise CandidatePersistenceError("notification_candidate_write_unconfirmed") from None



def receipt_notification_kind(report, *, quiet_eligible):
    if not isinstance(report.get("execution_receipt"), Mapping):
        return "receipt_missing"
    evidence = {**report, **(report.get("diagnostics") or {}), **(report.get("summary") or {})}
    classification = _kind(evidence, dry_run=report.get("dry_run") is True)
    if classification in {"auth_failure", "order_status_unknown", "blocked_run", "real_error"}:
        return classification
    return "healthy_heartbeat" if quiet_eligible else "receipt_unacceptable"

def monitor_message(kind):
    zh = str(os.environ.get("QSL_NOTIFY_LANG") or os.environ.get("NOTIFY_LANG") or "zh").startswith("zh")
    messages = {
        "receipt_missing": ("⚠️ 缺少执行回执；请检查最近一次运行。", "⚠️ Execution receipt missing; check the latest run."),
        "receipt_stale": ("⚠️ 执行回执已过期；请检查最近应运行的周期。", "⚠️ Execution receipt stale; check the latest due cycle."),
        "receipt_unacceptable": ("⚠️ 执行回执未满足验收要求；请检查运行证据。", "⚠️ Execution receipt unacceptable; review the run evidence."),
        "auth_failure": ("⚠️ 认证失败；请检查认证状态。", "⚠️ Authentication failed; check authentication status."),
        "real_error": ("⚠️ 运行检查失败；请检查运行记录。", "⚠️ Runtime check failed; review the run records."),
        "order_status_unknown": ("⚠️ 订单状态未决或未知；请核对订单状态。", "⚠️ Order status pending or unknown; reconcile the order status."),
        "blocked_run": ("⚠️ 本次运行被保护规则阻断；请核对运行证据。", "⚠️ Run blocked by a protection rule; review the run evidence."),
        "drill_error": ("⚠️ 模拟演练未通过核验；请检查运行证据。", "⚠️ Dry-run verification failed; review the run evidence."),
    }
    return messages.get(kind, messages["receipt_unacceptable"])[0 if zh else 1]


def publish_monitor(*, kind, send, report=None, target=None, now=None, family="receipt"):
    """Called only after the existing monitor has classified its original evidence."""
    if not enabled():
        return send()
    source = report or {}
    target = target or source.get("runtime_target")
    facts = (source.get("summary") or {}).get("notification_digest") or {}
    valid_facts = facts.get("schema_version") == "qsl.notification_cycle.v1" and facts.get("dry_run") is False
    event = event_for(kind, reason=kind, report=source, target=target, now=now,
                      order_count=facts.get("order_count") if valid_facts else None,
                      fill_count=facts.get("fill_count") if valid_facts else None)
    # A monitor observation is ordered by its check time. The candidate stays in
    # the original source run's business window; its incident uses fresh proof.
    from dataclasses import replace
    observed = replace(event, occurred_at=now or datetime.now(timezone.utc))
    _, values = _identity(target)
    state = _state_store(values)
    def immediate():
        return send_incident(observed, send, family=family, store=state)
    if report is None:
        # QRS has no non-counting monitor channel. No receipt means no business
        # run to attach a candidate to; keep only the durable immediate alert.
        return immediate()
    result = route_notification_event(
        event, env=os.environ, flag_key=FLAG,
        persist_candidate=lambda fact: _checked_candidate(fact, actually_ran=report is not None,
            dry_run=source.get("dry_run") is True, env=values,
            display_alias=display_account_alias(values), decisions=routine_decisions(source.get("summary") or {})),
        legacy=send, immediate=immediate,
    )
    if not result.candidate_persisted:
        print("notification_candidate_write_unconfirmed", flush=True)
    if kind == "healthy_heartbeat":
        recover_incidents(observed, family=family, store=state)
    return result.result is not False


def publish_runtime_failure(exc, send):
    if not enabled():
        return send()
    event = event_for("real_error", reason="runtime_setup:" + type(exc).__name__)
    try:
        return send_incident(event, send, family="runtime")
    except Exception:
        print("notification_runtime_marker_unavailable", flush=True)
        return False


def publish_risk_attention(decision, publish, *, profile, reasons=(), recovery_proven=False):
    """Observe notification severity only; never change the admission decision."""
    from dataclasses import replace
    rank = getattr(decision.level, "rank", None)
    rank = rank if isinstance(rank, int) else {"ok": 0, "watch": 1, "action": 2, "halt": 3}[decision.level.value]
    event = event_for("risk_escalation", reason="new_risk_attention:" + ",".join(sorted(str(v) for v in reasons)))
    event = replace(event, strategy_profile=profile)
    try:
        if rank < 2:
            if recovery_proven:
                recover_incidents(event, family="risk")
            return {"sent": 0, "skipped": 1, "failed": 0}
        counts = {"sent": 0, "skipped": 1, "failed": 0}
        def send():
            nonlocal counts
            counts = publish()
            return counts.get("sent", 0) > 0 and counts.get("failed", 0) == 0
        try:
            _checked_candidate(event.to_candidate(), actually_ran=_RUN.get() is not None, dry_run=False)
        except CandidatePersistenceError:
            print("notification_candidate_write_unconfirmed", flush=True)
        send_incident(event, send, family="risk", severity=rank)
        return counts
    except Exception:
        print("notification_attention_marker_unavailable", flush=True)
        return {"sent": 0, "skipped": 0, "failed": 1}


def persist_archived_reports(reports, env, *, now=None):
    target = json.loads(env["RUNTIME_TARGET_JSON"])
    required = {"platform_id", "strategy_profile", "service_name", "account_scope", "account_selector", "deployment_selector", "dry_run_only"}
    if (not required.issubset(target) or type(target["dry_run_only"]) is not bool
            or any(not isinstance(target[k], str) or not target[k].strip() for k in required - {"account_selector", "dry_run_only"})
            or not isinstance(target["account_selector"], list) or not target["account_selector"]
            or any(not isinstance(v, str) or not v.strip() for v in target["account_selector"])):
        raise ValueError("candidate_target_invalid")
    current = now or datetime.now(timezone.utc)
    from datetime import timedelta
    for report in reports:
        source_target = report.get("runtime_target")
        stamp = datetime.fromisoformat(str(report.get("started_at", "")).replace("Z", "+00:00"))
        if (report.get("schema_version") != "runtime_report.v1" or type(report.get("dry_run")) is not bool
                or not isinstance(source_target, Mapping) or any(source_target.get(k) != target[k] for k in required)
                or report.get("platform") not in {target["platform_id"], env.get("RUNTIME_HEARTBEAT_REPORT_PLATFORM", target["platform_id"])}
                or report.get("strategy_profile") != target["strategy_profile"] or report.get("service_name") != target["service_name"]
                or not isinstance(report.get("run_id"), str) or not report["run_id"]
                or stamp.tzinfo is None or not timedelta(0) <= current - stamp <= timedelta(hours=36)
                or report.get("status") not in {"ok", "completed", "success", "error", "aborted", "failed", "skipped"}
                or target["dry_run_only"] is True and report["dry_run"] is False):
            raise ValueError("candidate_source_invalid")
        summary = report.get("summary") or {}
        facts = summary.get("notification_digest") or {}
        kind = _kind({**summary, "errors": (report.get("error_summary") or {}).get("errors") or report.get("errors")}, dry_run=report["dry_run"])
        valid_facts = facts.get("schema_version") == "qsl.notification_cycle.v1" and facts.get("dry_run") is report["dry_run"]
        if report["status"] in {"error", "aborted", "failed"}:
            kind = "real_error"
        elif not valid_facts:
            kind = "receipt_unacceptable"
        reason = kind
        reason_code = str(summary.get("no_op_reason") or "").split(":", 1)[0]
        if re.fullmatch(r"[a-zA-Z0-9_]{1,100}", reason_code):
            reason += ":" + reason_code
        event = NotificationEvent(
            kind, datetime.fromisoformat(report["started_at"].replace("Z", "+00:00")), platform_id="ibkr",
            opaque_account_uid=env.get("DIGEST_OPAQUE_ACCOUNT_UID"), target_id=env.get("DIGEST_TARGET_ID"),
            strategy_profile=target["strategy_profile"], source_run_id=report["run_id"], incident_reason=reason,
            order_count=facts.get("order_count") if valid_facts and not report["dry_run"] else None,
            fill_count=facts.get("fill_count") if valid_facts and not report["dry_run"] else None,
        )
        if kind in {"order_submitted", "fill"}:
            from dataclasses import replace
            from notifications.digest_snapshot.unified_contract import order_identity_digest
            orders = list(summary.get("orders_submitted") or ()) + list(summary.get("option_orders_submitted") or ())
            ids = [next((str(order[k]) for k in ("permId", "orderId", "order_id", "execution_id") if order.get(k) is not None), "") for order in orders]
            if ids and all(ids):
                event = replace(event, order_execution_digest=order_identity_digest(json.dumps(sorted(ids))))
        from scripts.project_digest_candidates import _latest_notification_block_reason
        from quant_platform_kit.common.execution_receipts import attach_execution_receipt
        def validate_receipt(source):
            return attach_execution_receipt(dict(source), source.get("execution_receipt"))["execution_receipt"]
        block_reason = _latest_notification_block_reason([report], validate_receipt)
        if not _checked_candidate(event.to_candidate(), env=env, actually_ran=True,
                                  dry_run=report["dry_run"], block_reason=block_reason,
                                  display_alias=display_account_alias(env), decisions=routine_decisions(summary)):
            raise CandidatePersistenceError("notification_candidate_write_unconfirmed")
    return {"status": "projected", "uploaded": True, "runs": len(reports)}
