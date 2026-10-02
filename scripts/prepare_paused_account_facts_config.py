"""Purely assemble the existing IBKR paused-target adopter and probe configs."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


ADOPT_SCHEMA = "qsl_ibkr_metadata_adopt_config.v1"
PROBE_SCHEMA = "ibkr_additional_balance_probe.v1"
CONTEXT_KEYS = {
    "project", "region", "service", "service_resource", "service_base",
    "production_revision", "old_revision", "old_source",
    "allowed_candidate_source", "oidc", "report_prefix", "expected_job",
}
BINDING_KEYS = {
    "account_scope", "account_selector", "deployment_selector", "project_id",
    "runtime_revision", "service_name", "source_binding_id",
}
ANCHOR_KEYS = {
    "request_started_at_utc", "request_finished_at_utc", "request_url", "revision",
    "report_identity_matches_binding", "orders_submitted_count", "receipt_outcome",
    "private_evidence_ref",
}
CONTEXT_JOB_KEYS = {"name", "schedule", "method", "path", "deadline"}
ROLES = {"main", "precheck", "warmup"}
SLOT_ORDINALS = {"additional-1": 0, "additional-2": 2, "additional-3": 3}
MAX_EVIDENCE_AGE = timedelta(seconds=120)
MAX_PAUSED_ANCHOR_AGE = timedelta(days=30)
WINDOW_SECONDS = 600
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class PreparationError(ValueError):
    """A fixed, safe-to-report preparation failure category."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _fail(category: str) -> None:
    raise PreparationError(category)


def _text(value: object, category: str = "input_invalid") -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        _fail(category)
    return value


def _timestamp(value: object, category: str = "time_invalid") -> datetime:
    if not isinstance(value, str) or not value:
        _fail(category)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        _fail(category)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(category)
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _revision_short(value: object, service_resource: str, service: str) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    prefix = f"{service_resource}/revisions/"
    if value.startswith("projects/"):
        if not value.startswith(prefix):
            return None
        value = value[len(prefix):]
    if not isinstance(value, str) or "/" in value or not value.startswith(service + "-"):
        return None
    return value


def _binding_id(binding: dict[str, object]) -> str:
    selector = binding.get("account_selector")
    if (
        not isinstance(selector, list) or len(selector) != 1
        or not isinstance(selector[0], str) or not selector[0].strip()
        or selector[0].strip().lower() == "default"
    ):
        _fail("account_binding_invalid")
    canonical = {
        "account_scope": binding.get("account_scope"),
        "account_selector": selector,
        "deployment_selector": binding.get("deployment_selector"),
        "project_id": binding.get("project_id"),
        "runtime_revision": binding.get("runtime_revision"),
        "service_name": binding.get("service_name"),
    }
    for key in ("account_scope", "deployment_selector", "project_id", "runtime_revision", "service_name"):
        _text(canonical[key], "account_binding_invalid")
    payload = json.dumps(canonical, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_context(inputs: dict[str, object], observer_readonly: bool = False) -> tuple[dict[str, object], dict[str, object]]:
    required_inputs = {
        "target_context", "image_digest", "account_binding",
        "selected_slot", "old_target", "live_jobs",
    }
    expected = required_inputs if observer_readonly else required_inputs | {"paused_prior_run"}
    if observer_readonly and "paused_prior_run" in inputs:
        _fail("paused_anchor_invalid")
    if set(inputs) != expected or inputs.get("selected_slot") not in SLOT_ORDINALS:
        _fail("target_input_invalid")
    context = inputs.get("target_context")
    binding = inputs.get("account_binding")
    if not isinstance(context, dict) or set(context) != CONTEXT_KEYS:
        _fail("target_context_invalid")
    if not isinstance(binding, dict) or set(binding) != BINDING_KEYS:
        _fail("account_binding_invalid")
    for key in ("project", "region", "service", "service_resource", "service_base", "production_revision", "old_revision", "old_source", "allowed_candidate_source", "oidc", "report_prefix"):
        _text(context[key], "target_context_invalid")
    service = context["service"]
    project = context["project"]
    if not _SAFE_NAME.fullmatch(service) or not _SAFE_NAME.fullmatch(context["region"]):
        _fail("target_context_invalid")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        _fail("target_context_invalid")
    expected_resource = f"projects/{project}/locations/{context['region']}/services/{service}"
    if context["service_resource"] != expected_resource:
        _fail("target_context_invalid")
    parsed_base = urlsplit(context["service_base"])
    if parsed_base.scheme != "https" or not parsed_base.hostname or parsed_base.path or parsed_base.username or parsed_base.password or parsed_base.query or parsed_base.fragment:
        _fail("target_context_invalid")
    if not _HEX40.fullmatch(context["old_source"]) or not _HEX40.fullmatch(context["allowed_candidate_source"]):
        _fail("target_context_invalid")
    if (
        context["production_revision"] == context["old_revision"]
        or _revision_short(context["production_revision"], expected_resource, service) != context["production_revision"]
        or _revision_short(context["old_revision"], expected_resource, service) != context["old_revision"]
    ):
        _fail("target_context_invalid")
    jobs = context.get("expected_job")
    if not isinstance(jobs, dict) or set(jobs) != ROLES:
        _fail("target_context_invalid")
    for role, spec in jobs.items():
        if not isinstance(spec, dict) or set(spec) != CONTEXT_JOB_KEYS:
            _fail("target_context_invalid")
        for value in spec.values():
            _text(value, "target_context_invalid")
        if "/" in spec["name"] or spec["method"] != {"main": "POST", "precheck": "POST", "warmup": "GET"}[role]:
            _fail("target_context_invalid")
        if not spec["path"].startswith("/") or ".." in spec["path"] or ":" in spec["path"]:
            _fail("target_context_invalid")
    if {spec["path"] for spec in jobs.values()} != {"/run", "/dry-run", "/health"}:
        _fail("target_context_invalid")

    digest = inputs.get("image_digest")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        _fail("candidate_digest_invalid")
    if (
        binding.get("project_id") != project
        or binding.get("service_name") != service
        or binding.get("runtime_revision") != context["production_revision"]
        or binding.get("source_binding_id") != _binding_id(binding)
    ):
        _fail("account_binding_invalid")
    _text(binding.get("account_scope"), "account_binding_invalid")
    _text(binding.get("deployment_selector"), "account_binding_invalid")
    old_target = inputs.get("old_target")
    if not isinstance(old_target, dict) or any(
        old_target.get(key) != binding.get(key)
        for key in ("project_id", "service_name", "account_scope", "account_selector", "deployment_selector")
    ):
        _fail("account_binding_invalid")
    return context, binding


def _traffic(rows: object, context: dict[str, object]) -> dict[str, int]:
    if not isinstance(rows, list) or not rows:
        _fail("service_traffic_invalid")
    traffic: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("tag"):
            _fail("service_traffic_invalid")
        revision = _revision_short(row.get("revision"), context["service_resource"], context["service"])
        percent = row.get("percent", 0)
        if revision is None or type(percent) is not int or not 0 <= percent <= 100 or revision in traffic:
            _fail("service_traffic_invalid")
        traffic[revision] = percent
    return traffic


def _check_service(
    service: dict[str, object], old_revision: dict[str, object], candidate: dict[str, object],
    context: dict[str, object], binding: dict[str, object], digest: str,
) -> None:
    if (
        service.get("name") != context["service_resource"]
        or service.get("reconciling", False) is not False
        or not isinstance(service.get("terminalCondition"), dict)
        or service["terminalCondition"].get("state") != "CONDITION_SUCCEEDED"
    ):
        _fail("service_not_reconciled")
    statuses = _traffic(service.get("trafficStatuses"), context)
    desired = _traffic(service.get("traffic"), context)
    old_name = context["old_revision"]
    candidate_name = context["production_revision"]
    if (
        statuses.get(old_name) != 100 or statuses.get(candidate_name, 0) != 0
        or sum(percent == 100 for percent in statuses.values()) != 1
        or any(percent not in (0, 100) for percent in statuses.values())
        or desired.get(old_name) != 100 or desired.get(candidate_name, 0) != 0
        or any(name not in (old_name, candidate_name) and percent != 0 for name, percent in desired.items())
    ):
        _fail("service_traffic_invalid")
    _check_revision(old_revision, old_name, context, binding, context["old_source"], None)
    _check_revision(candidate, candidate_name, context, binding, context["allowed_candidate_source"], digest)


def _check_revision(
    revision: dict[str, object], expected: str, context: dict[str, object],
    binding: dict[str, object], source_sha: str, digest: str | None,
) -> None:
    resource = context["service_resource"]
    service = context["service"]
    if _revision_short(revision.get("name"), resource, service) != expected:
        _fail("revision_identity_invalid")
    labels = revision.get("labels")
    if not isinstance(labels, dict) or labels.get("commit-sha") != source_sha:
        _fail("revision_source_invalid")
    conditions = revision.get("conditions")
    if not isinstance(conditions, list) or not any(
        isinstance(row, dict) and row.get("type") == "Ready" and row.get("state") == "CONDITION_SUCCEEDED"
        for row in conditions
    ):
        _fail("revision_not_ready")
    containers = revision.get("containers")
    if not isinstance(containers, list) or not containers or any(not isinstance(row, dict) for row in containers):
        _fail("revision_invalid")
    images = [row.get("image") for row in containers]
    if any(not isinstance(image, str) for image in images):
        _fail("revision_invalid")
    if digest is not None and not images[0].endswith("@sha256:" + digest):
        _fail("candidate_digest_invalid")
    env_values: dict[str, list[object]] = {}
    for container in containers:
        env = container.get("env", [])
        if not isinstance(env, list):
            _fail("revision_invalid")
        for item in env:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                _fail("revision_invalid")
            env_values.setdefault(item["name"], []).append(item.get("value"))
    for force_name in ("QSL_IBKR_FORCE_RUN", "IBKR_FORCE_RUN"):
        if any(value not in (None, "false", "False", "0", "") for value in env_values.get(force_name, [])):
            _fail("execution_force_enabled")
    target_values = env_values.get("RUNTIME_TARGET_ENABLED", [])
    if not target_values or any(value not in ("false", "False", "0") for value in target_values):
        _fail("runtime_target_enabled")
    raw_targets = env_values.get("RUNTIME_TARGET_JSON", [])
    if not raw_targets:
        _fail("runtime_target_identity_invalid")
    for raw_target in raw_targets:
        try:
            target = json.loads(raw_target) if isinstance(raw_target, str) else None
        except (ValueError, TypeError):
            _fail("runtime_target_identity_invalid")
        if not isinstance(target, dict) or any(
            target.get(key) != binding.get(key)
            for key in ("account_selector", "account_scope", "deployment_selector")
        ):
            _fail("runtime_target_identity_invalid")


def _check_jobs(jobs: dict[str, object], context: dict[str, object]) -> None:
    if not isinstance(jobs, dict) or set(jobs) != ROLES:
        _fail("jobs_invalid")
    base = context["service_base"]
    oidc = context["oidc"]
    for role in ("main", "precheck", "warmup"):
        expected = context["expected_job"][role]
        job = jobs[role]
        if not isinstance(job, dict):
            _fail("job_invalid")
        target = job.get("httpTarget")
        token = target.get("oidcToken") if isinstance(target, dict) else None
        resource = f"projects/{context['project']}/locations/{context['region']}/jobs/{expected['name']}"
        if (
            job.get("name") != resource
            or job.get("state") not in (("PAUSED", "ENABLED") if role == "precheck" else ("PAUSED",))
            or job.get("schedule") != expected["schedule"]
            or job.get("timeZone") != "America/New_York"
            or job.get("attemptDeadline") != expected["deadline"]
            or not isinstance(target, dict) or target.get("uri") != base + expected["path"]
            or target.get("httpMethod") != expected["method"]
            or not isinstance(token, dict) or token.get("serviceAccountEmail") != oidc
            or token.get("audience") != base
        ):
            _fail("job_preflight_failed")
        if role == "precheck":
            retry = job.get("retryConfig", {})
            if (
                not isinstance(retry, dict)
                or type(retry.get("retryCount")) is not int
                or retry.get("retryCount") != 3
                or retry.get("maxRetryDuration") != "900s"
            ):
                _fail("job_preflight_failed")


def _check_anchor(anchor: object, context: dict[str, object], binding: dict[str, object], now: datetime) -> dict[str, object]:
    if not isinstance(anchor, dict) or set(anchor) != ANCHOR_KEYS:
        _fail("paused_anchor_invalid")
    started = _timestamp(anchor.get("request_started_at_utc"), "paused_anchor_invalid")
    finished = _timestamp(anchor.get("request_finished_at_utc"), "paused_anchor_invalid")
    if (
        started > finished or finished > now
        or now - started > MAX_PAUSED_ANCHOR_AGE
        or anchor.get("request_url") != context["service_base"] + context["expected_job"]["main"]["path"]
        or not isinstance(anchor.get("revision"), str)
        or not anchor["revision"].startswith(context["service"] + "-")
        or "/" in anchor["revision"]
        or anchor.get("report_identity_matches_binding") is not True
        or type(anchor.get("orders_submitted_count")) is not int
        or anchor["orders_submitted_count"] != 0
        or anchor.get("receipt_outcome") != "no_action"
        or not isinstance(anchor.get("private_evidence_ref"), str)
        or not anchor["private_evidence_ref"].startswith(context["report_prefix"].rstrip("/") + "/")
    ):
        _fail("paused_anchor_invalid")
    return copy.deepcopy(anchor)


def _check_gateway(mapping: dict[str, object], passive: dict[str, object], now: datetime, selected_index: object, expected_ordinal: int, observer_readonly: bool = False) -> datetime:
    identity_flag = (
        "current_vm_target_matches_actual_runtime_config" if observer_readonly
        else "current_vm_host_matches_actual_runtime_config"
    )
    mapping_keys = {
        "status", "runtime_ordinal", "gateway_inventory_index", "native_account_matches_protected",
        identity_flag, "mode_matches", "vm_running", "observed_at_utc", "mutations",
    }
    passive_keys = {
        "status", "container_running", "api_listener", "established_api_connections", "observed_at_utc",
        "account_authentication", "pending_orders", "broker_calls", "production_mutations",
    }
    if not isinstance(mapping, dict) or set(mapping) != mapping_keys:
        _fail("gateway_mapping_invalid")
    if not isinstance(passive, dict) or set(passive) != passive_keys:
        _fail("gateway_observation_invalid")
    index = mapping.get("gateway_inventory_index")
    if (
        mapping.get("status") != "mapping_verified"
        or type(mapping.get("runtime_ordinal")) is not int or mapping["runtime_ordinal"] != expected_ordinal
        or type(index) is not int or index < 0
        or type(selected_index) is not int or selected_index != index
        or any(mapping.get(flag) is not True for flag in (
            "native_account_matches_protected", identity_flag, "mode_matches", "vm_running",
        ))
        or type(mapping.get("mutations")) is not int or mapping["mutations"] != 0
    ):
        _fail("gateway_mapping_invalid")
    if (
        passive.get("status") != "observed"
        or passive.get("container_running") is not True or passive.get("api_listener") is not True
        or type(passive.get("established_api_connections")) is not int or passive["established_api_connections"] != 0
        or type(passive.get("broker_calls")) is not int or passive["broker_calls"] != 0
        or type(passive.get("production_mutations")) is not int or passive["production_mutations"] != 0
        or passive.get("account_authentication") != "not_verified"
        or passive.get("pending_orders") != "not_verified"
    ):
        _fail("gateway_observation_invalid")
    mapping_time = _timestamp(mapping.get("observed_at_utc"), "gateway_mapping_invalid")
    passive_time = _timestamp(passive.get("observed_at_utc"), "gateway_observation_invalid")
    if (
        mapping_time > now or passive_time > now
        or now - mapping_time > MAX_EVIDENCE_AGE or now - passive_time > MAX_EVIDENCE_AGE
    ):
        _fail("gateway_evidence_stale")
    return passive_time


def assemble_configs(
    paused_inputs: dict[str, object],
    service: dict[str, object],
    old_revision: dict[str, object],
    candidate_revision: dict[str, object],
    jobs: dict[str, object],
    recent_all_revision_requests: list[object],
    gateway_mapping: dict[str, object],
    passive_observation: dict[str, object],
    actual_source_run_id: str,
    actual_selected_gateway_index: int,
    now: datetime,
    metadata_driver_sha256: str,
    probe_driver_sha256: str,
    sole_operator_confirmed: bool,
    *,
    pagination_complete: bool = False,
    actual_runtime_ordinal: int = 0,
    observer_readonly: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return the two existing strict configs when all original evidence matches."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _fail("time_invalid")
    now = now.astimezone(timezone.utc)
    if sole_operator_confirmed is not True:
        _fail("sole_operator_not_confirmed")
    for digest in (metadata_driver_sha256, probe_driver_sha256):
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            _fail("driver_source_hash_invalid")
    if observer_readonly is not True and observer_readonly is not False:
        _fail("observer_mode_invalid")
    context, binding = _validate_context(paused_inputs, observer_readonly)
    if (
        type(actual_runtime_ordinal) is not int
        or SLOT_ORDINALS.get(paused_inputs["selected_slot"]) != actual_runtime_ordinal
    ):
        _fail("gateway_mapping_invalid")
    _check_service(
        service, old_revision, candidate_revision, context,
        paused_inputs["account_binding"], paused_inputs["image_digest"],
    )
    _check_jobs(jobs, context)
    if pagination_complete is not True or not isinstance(recent_all_revision_requests, list) or recent_all_revision_requests:
        _fail("recent_requests_not_clear")
    passive_time = _check_gateway(
        gateway_mapping, passive_observation, now, actual_selected_gateway_index,
        actual_runtime_ordinal, observer_readonly,
    )
    if not isinstance(actual_source_run_id, str) or not re.fullmatch(r"[1-9][0-9]*", actual_source_run_id):
        _fail("gateway_evidence_invalid")
    anchor = None if observer_readonly else _check_anchor(paused_inputs["paused_prior_run"], context, binding, now)
    checked = _format_time(now)
    proof = {
        "service": context["service"],
        "evidence_id": actual_source_run_id,
        "cloudrun_no_inflight": True,
        "gateway_client_no_concurrent": True,
        "checked_at_utc": checked,
        "window_start_utc": checked,
        "window_end_utc": _format_time(now + timedelta(seconds=WINDOW_SECONDS)),
        "gateway_observed_at_utc": _format_time(passive_time),
        "gateway_target_matches": True,
    }
    if observer_readonly:
        proof["observer_readonly"] = True
    else:
        proof["paused_prior_run"] = anchor
    common = {
        "target_context": copy.deepcopy(context),
        "image_digest": paused_inputs["image_digest"],
        "account_binding": copy.deepcopy(binding),
        "window_proof": proof,
        "confirm_no_other_writers": True,
    }
    adopter = {
        "schema": ADOPT_SCHEMA,
        "driver_sha256": metadata_driver_sha256,
        **copy.deepcopy(common),
        "source_sha": context["allowed_candidate_source"],
    }
    probe = {
        "schema": PROBE_SCHEMA,
        "driver_sha256": probe_driver_sha256,
        **copy.deepcopy(common),
    }
    return adopter, probe
