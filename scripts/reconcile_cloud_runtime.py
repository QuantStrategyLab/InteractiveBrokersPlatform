#!/usr/bin/env python3
"""Reconcile Cloud Run and Cloud Scheduler runtime drift.

The script intentionally only auto-fixes infrastructure drift that is safe to
repair: Cloud Run traffic pointing at an older revision and known legacy
Scheduler jobs. Runtime code errors, secret gaps, and live/paper policy
conflicts must still fail fast in the owning workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class RuntimeTarget:
    service_name: str
    region: str = ""
    account_scope: str = ""


class ReconcileError(RuntimeError):
    pass


def _switch_targets_match(actual: object, desired: object, *, disabling: bool) -> bool:
    if not isinstance(actual, Mapping) or not isinstance(desired, Mapping):
        return False
    if not disabling:
        return actual == desired
    actual_scheduler, desired_scheduler = actual.get("scheduler"), desired.get("scheduler")
    if not isinstance(actual_scheduler, Mapping) or not isinstance(desired_scheduler, Mapping):
        return actual == desired
    a, b = dict(actual), dict(desired)
    a["scheduler"] = {key: value for key, value in actual_scheduler.items() if key != "precheck_time"}
    b["scheduler"] = {key: value for key, value in desired_scheduler.items() if key != "precheck_time"}
    return a == b


def validate_switch_baseline(revision: Mapping[str, Any], expected: Mapping[str, Any]) -> tuple[str, bool]:
    """Validate an existing serving revision and return the desired switch and no-op status."""
    from scripts.runtime_target_continuity import verify_traffic_revision

    meta = revision.get("metadata") or {}
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        raise ReconcileError("switch baseline revision is missing")
    containers = revision.get("spec", {}).get("containers") or []
    if len(containers) != 1 or not containers[0].get("image"):
        raise ReconcileError("switch baseline container is invalid")
    rows = containers[0].get("env") or []
    names = [row.get("name") for row in rows]
    if len(names) != len(set(names)):
        raise ReconcileError("switch baseline environment is ambiguous")
    actual = {row.get("name"): row.get("value") for row in rows}
    desired = expected.get("env") or {}
    value = desired.get("RUNTIME_TARGET_ENABLED")
    if value not in ("true", "false") or actual.get("RUNTIME_TARGET_ENABLED") not in ("true", "false"):
        raise ReconcileError("switch must have an explicit boolean value")
    try:
        actual_target = json.loads(actual.get("RUNTIME_TARGET_JSON") or "null")
        desired_target = json.loads(desired.get("RUNTIME_TARGET_JSON") or "null")
    except (TypeError, ValueError) as exc:
        raise ReconcileError("switch target is invalid") from exc
    if not _switch_targets_match(actual_target, desired_target, disabling=value == "false"):
        raise ReconcileError("switch target does not match serving target")
    if value == "true" and expected.get("standard_execution_enabled") is not True:
        raise ReconcileError("switch target does not permit normal execution")
    if value == "true":
        verify_traffic_revision(revision, expected_revision=name, expected_commit=(meta.get("labels") or {}).get("commit-sha"))
    return value, actual["RUNTIME_TARGET_ENABLED"] == value


def _without_runtime_switch(spec: Mapping[str, Any], *, unnamed_template: bool = False) -> dict[str, Any]:
    import copy

    result = copy.deepcopy(dict(spec))
    result.pop("servingState", None)  # Cloud Run lifecycle output, not configuration.
    for container in result.get("containers") or []:
        if unnamed_template:
            container.pop("name", None)
        container["env"] = sorted(
            (row for row in container.get("env") or [] if row.get("name") != "RUNTIME_TARGET_ENABLED"),
            key=lambda row: row.get("name", ""),
        )
    return result


def validate_initial_state_baseline(revision: Mapping[str, Any], expected: Mapping[str, Any],
                                    ledger: Mapping[str, Any], *, now=None) -> dict[str, str]:
    """Derive the confirmed transition from the actual disabled serving target."""
    from scripts.reconciliation_recovery_state_ledger import apply_recovery_state_ledger
    from datetime import datetime, timezone
    from quant_platform_kit.common.reconciliation_recovery import DEFAULT_RECONCILIATION_RECOVERY_MAX_AGE

    if not str((revision.get("metadata") or {}).get("name") or "").startswith(str(expected.get("service_name") or "") + "-"):
        raise ReconcileError("initial baseline service does not match")
    containers = revision.get("spec", {}).get("containers") or []
    if len(containers) != 1 or not containers[0].get("image"):
        raise ReconcileError("initial baseline container is invalid")
    rows = containers[0].get("env") or []
    names = [row.get("name") for row in rows]
    if len(names) != len(set(names)):
        raise ReconcileError("initial baseline environment is ambiguous")
    values = {row.get("name"): row.get("value") for row in rows}
    desired = expected.get("env") or {}
    if values.get("RUNTIME_TARGET_ENABLED") != "false" or desired.get("RUNTIME_TARGET_ENABLED") != "false":
        raise ReconcileError("initial baseline must remain disabled")
    try:
        actual = json.loads(values.get("RUNTIME_TARGET_JSON") or "null")
        derived, digests = apply_recovery_state_ledger(runtime_target=actual, ledger=ledger)
        verified_at = datetime.fromisoformat(str(ledger["transition_plan"]["verified_at"]).replace("Z", "+00:00"))
        if verified_at.utcoffset() is None:
            raise ValueError("missing timezone")
        current = now or datetime.now(timezone.utc)
        if current < verified_at or current - verified_at > DEFAULT_RECONCILIATION_RECOVERY_MAX_AGE:
            raise ValueError("verification expired")
    except (TypeError, ValueError) as exc:
        raise ReconcileError("initial baseline or ledger is invalid") from exc
    if expected.get("service_name") != ledger.get("service_name"):
        raise ReconcileError("initial confirmed transition does not match serving target")
    return {"RUNTIME_TARGET_JSON": json.dumps(derived, sort_keys=True, separators=(",", ":")),
            "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON": json.dumps(digests, sort_keys=True, separators=(",", ":"))}


def _without_initial_state(spec: Mapping[str, Any], *, unnamed_template: bool = False) -> dict[str, Any]:
    import copy
    result = copy.deepcopy(dict(spec))
    result.pop("servingState", None)
    for container in result.get("containers") or []:
        if unnamed_template:
            container.pop("name", None)
        container["env"] = sorted((row for row in container.get("env") or [] if row.get("name") not in {
            "RUNTIME_TARGET_JSON", "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON"}), key=lambda row: row.get("name", ""))
    return result


def build_initial_state_stage(service: Mapping[str, Any], revision: Mapping[str, Any],
                              updates: Mapping[str, str], candidate_revision: str) -> dict[str, Any]:
    """Build one version-conditional service replacement without changing traffic."""
    import copy
    metadata = service.get("metadata") or {}
    if set(updates) != {"RUNTIME_TARGET_JSON", "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON"}:
        raise ReconcileError("initial state fields are invalid")
    if not metadata.get("resourceVersion") or not candidate_revision.startswith(str(metadata.get("name") or "") + "-"):
        raise ReconcileError("initial service identity is invalid")
    template = service.get("spec", {}).get("template", {})
    template_containers = template.get("spec", {}).get("containers") or []
    if len(template_containers) != 1:
        raise ReconcileError("initial service template is invalid")
    unnamed = not template_containers[0].get("name")
    if _without_initial_state(template.get("spec", {}), unnamed_template=unnamed) != _without_initial_state(revision.get("spec", {}), unnamed_template=unnamed):
        raise ReconcileError("initial serving template changed")
    def selected(container):
        rows = container.get("env") or []
        if len(rows) != len({row.get("name") for row in rows}):
            raise ReconcileError("initial service environment is ambiguous")
        return {row["name"]: row for row in rows if row.get("name") in {
            "RUNTIME_TARGET_JSON", "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON"}}
    revision_containers = revision.get("spec", {}).get("containers") or []
    if len(revision_containers) != 1 or selected(template_containers[0]) != selected(revision_containers[0]):
        raise ReconcileError("initial serving environment changed")
    traffic = service.get("status", {}).get("traffic") or []
    if not _traffic_on_revision(service, (revision.get("metadata") or {}).get("name")):
        raise ReconcileError("initial serving traffic changed")
    frozen = []
    for row in traffic:
        if not isinstance(row.get("revisionName"), str) or type(row.get("percent", 0)) is not int:
            raise ReconcileError("initial service traffic is invalid")
        item = {"revisionName": row["revisionName"], "percent": row.get("percent", 0)}
        if row.get("tag"):
            item["tag"] = row["tag"]
        frozen.append(item)
    body = {"apiVersion": "serving.knative.dev/v1", "kind": "Service",
            "metadata": {key: copy.deepcopy(value) for key, value in metadata.items()
                         if key in {"name", "namespace", "resourceVersion", "labels", "annotations"}},
            "spec": copy.deepcopy(service["spec"])}
    body["spec"]["traffic"] = frozen
    body["spec"]["template"].setdefault("metadata", {})["name"] = candidate_revision
    env = body["spec"]["template"]["spec"]["containers"][0].setdefault("env", [])
    for key, value in updates.items():
        found = [row for row in env if row.get("name") == key]
        if found and (len(found) != 1 or "valueFrom" in found[0]):
            raise ReconcileError("initial state field is not a plain value")
        if found:
            found[0]["value"] = value
        else:
            env.append({"name": key, "value": value})
    return body


def _adopt_initial_candidate(service: Mapping[str, Any], *, project: str, region: str,
                             candidate_revision: str) -> None:
    """Adopt the verified candidate with the current Cloud Run resourceVersion."""
    import copy
    from urllib.parse import quote
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    metadata = service.get("metadata") or {}
    if not metadata.get("resourceVersion") or not metadata.get("name"):
        raise ReconcileError("initial adoption service version is unavailable")
    tags = []
    for row in service.get("status", {}).get("traffic") or []:
        if row.get("tag"):
            if not isinstance(row.get("revisionName"), str):
                raise ReconcileError("initial adoption tagged traffic is invalid")
            tags.append({"tag": row["tag"], "revisionName": row["revisionName"], "percent": 0})
    body = {"apiVersion": "serving.knative.dev/v1", "kind": "Service",
            "metadata": {key: copy.deepcopy(value) for key, value in metadata.items()
                         if key in {"name", "namespace", "resourceVersion", "labels", "annotations"}},
            "spec": copy.deepcopy(service["spec"])}
    body["spec"]["traffic"] = [{"revisionName": candidate_revision, "percent": 100}, *tags]
    token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=30, check=False)
    if token.returncode or not token.stdout.strip():
        raise ReconcileError("initial adoption identity unavailable")
    url = (f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/"
           f"namespaces/{quote(project, safe='')}/services/{quote(str(metadata['name']), safe='')}")
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs): return None
    request = Request(url, method="PUT", data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + token.stdout.strip(), "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect).open(request, timeout=180) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError("unconfirmed")
    except Exception:
        raise ReconcileError("initial adoption failed or unknown") from None


def _load_json_object(raw: str, *, field_name: str) -> Mapping[str, Any] | list[Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReconcileError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(value, (Mapping, list)):
        raise ReconcileError(f"{field_name} must decode to an object or list")
    return value


def _runtime_target(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = entry.get("runtime_target") or entry.get("runtime_target_json")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        loaded = _load_json_object(raw, field_name="runtime_target_json")
        if isinstance(loaded, Mapping):
            return loaded
    return {}


def _target_from_entry(entry: Mapping[str, Any]) -> RuntimeTarget | None:
    runtime_target = _runtime_target(entry)
    service = (
        entry.get("service_name")
        or entry.get("service")
        or entry.get("cloud_run_service")
        or runtime_target.get("service_name")
    )
    service_name = str(service or "").strip()
    if not service_name:
        return None
    account_scope = str(
        entry.get("ACCOUNT_GROUP")
        or entry.get("account_scope")
        or runtime_target.get("account_scope")
        or ""
    ).strip()
    region = str(entry.get("region") or entry.get("cloud_run_region") or "").strip()
    return RuntimeTarget(service_name=service_name, region=region, account_scope=account_scope)


def _dedupe_targets(targets: Iterable[RuntimeTarget]) -> list[RuntimeTarget]:
    by_service: dict[str, RuntimeTarget] = {}
    for target in targets:
        current = by_service.get(target.service_name)
        if current is None:
            by_service[target.service_name] = target
            continue
        by_service[target.service_name] = RuntimeTarget(
            service_name=target.service_name,
            region=current.region or target.region,
            account_scope=current.account_scope or target.account_scope,
        )
    return list(by_service.values())


def load_targets(*, env: Mapping[str, str]) -> list[RuntimeTarget]:
    targets: list[RuntimeTarget] = []

    raw_plan = str(env.get("SYNC_PLAN_JSON") or "").strip()
    if raw_plan:
        plan = _load_json_object(raw_plan, field_name="SYNC_PLAN_JSON")
        entries = plan.get("targets") if isinstance(plan, Mapping) else plan
        if not isinstance(entries, list):
            raise ReconcileError("SYNC_PLAN_JSON.targets must be a list")
        for entry in entries:
            if isinstance(entry, Mapping):
                target = _target_from_entry(entry)
                if target:
                    targets.append(target)

    raw_targets = str(env.get("CLOUD_RUN_SERVICE_TARGETS_JSON") or "").strip()
    if raw_targets:
        payload = _load_json_object(raw_targets, field_name="CLOUD_RUN_SERVICE_TARGETS_JSON")
        entries = payload.get("targets") if isinstance(payload, Mapping) else payload
        if not isinstance(entries, list):
            raise ReconcileError("CLOUD_RUN_SERVICE_TARGETS_JSON.targets must be a list")
        for entry in entries:
            if isinstance(entry, Mapping):
                target = _target_from_entry(entry)
                if target:
                    targets.append(target)

    raw_services = str(env.get("CLOUD_RUN_SERVICES") or env.get("CLOUD_RUN_SERVICE") or "").strip()
    for chunk in raw_services.replace(";", ",").replace("\n", ",").split(","):
        service_name = chunk.strip()
        if service_name:
            targets.append(RuntimeTarget(service_name=service_name))

    return _dedupe_targets(targets)


def _run(args: Sequence[str], *, json_output: bool = False, dry_run: bool = False) -> Any:
    printable = " ".join(args)
    if dry_run:
        print(f"DRY-RUN {printable}")
        return {} if json_output else ""
    completed = subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ReconcileError(f"Command failed ({completed.returncode}): {printable}\n{detail}")
    if json_output:
        try:
            return json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ReconcileError(f"Command did not return JSON: {printable}") from exc
    return completed.stdout


def _run_optional(args: Sequence[str], *, dry_run: bool = False) -> bool:
    printable = " ".join(args)
    if dry_run:
        print(f"DRY-RUN {printable}")
        return True
    completed = subprocess.run(
        list(args),
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _traffic_on_revision(service: Mapping[str, Any], revision_name: str) -> bool:
    traffic = service.get("status", {}).get("traffic") or []
    if not isinstance(traffic, list):
        return False
    for item in traffic:
        if not isinstance(item, Mapping):
            continue
        try:
            percent = int(item.get("percent") or 0)
        except (TypeError, ValueError):
            percent = 0
        if percent == 100 and str(item.get("revisionName") or "").strip() == revision_name:
            return True
    return False


def _revision_is_ready(revision: Mapping[str, Any]) -> bool:
    conditions = revision.get("status", {}).get("conditions") or []
    if not isinstance(conditions, list):
        return False
    for condition in conditions:
        if not isinstance(condition, Mapping):
            continue
        if str(condition.get("type") or "") == "Ready" and str(condition.get("status") or "") == "True":
            return True
    return False


def _resolve_revision_for_commit(
    *,
    project: str,
    region: str,
    service_name: str,
    expected_commit: str,
    dry_run: bool,
) -> str:
    if dry_run:
        return f"{service_name}-dry-run"
    if not expected_commit:
        raise ReconcileError(f"expected commit is required to route traffic for {service_name}")
    revisions = _run(
        [
            "gcloud",
            "run",
            "revisions",
            "list",
            f"--service={service_name}",
            f"--project={project}",
            f"--region={region}",
            "--format=json",
        ],
        json_output=True,
        dry_run=dry_run,
    )
    if not isinstance(revisions, list):
        raise ReconcileError(f"Unable to list revisions for {service_name}")
    for revision in revisions:
        if not isinstance(revision, Mapping):
            continue
        if not _revision_is_ready(revision):
            continue
        labels = (revision.get("metadata") or {}).get("labels") or {}
        commit = str(labels.get("commit-sha") or "").strip()
        if commit != expected_commit:
            continue
        name = str((revision.get("metadata") or {}).get("name") or "").strip()
        if name:
            return name
    raise ReconcileError(
        f"No Ready revision for {service_name} with commit {expected_commit!r}"
    )


def ensure_latest_traffic(
    *,
    project: str,
    region: str,
    targets: Sequence[RuntimeTarget],
    expected_commit: str,
    dry_run: bool,
    env_sync_baseline: Mapping[str, Any] | None = None,
    env_sync_revision: str | None = None,
    env_sync_expected: Mapping[str, str] | None = None,
    switch_only: bool = False,
    initial_state_only: bool = False,
    env_sync_service: Mapping[str, Any] | None = None,
) -> None:
    if env_sync_baseline is not None and (len(targets) != 1 or not env_sync_revision
                                          or not isinstance(env_sync_expected, Mapping) or dry_run):
        raise ReconcileError("env-only traffic requires one target and exact revision evidence")
    for target in targets:
        target_region = target.region or region
        if not target_region:
            raise ReconcileError(f"Region is required for {target.service_name}")
        service = _run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                target.service_name,
                f"--project={project}",
                f"--region={target_region}",
                "--format=json",
            ],
            json_output=True,
            dry_run=dry_run,
        )
        target_revision = env_sync_revision if env_sync_baseline is not None else _resolve_revision_for_commit(
            project=project, region=target_region, service_name=target.service_name,
            expected_commit=expected_commit, dry_run=dry_run,
        )
        if env_sync_baseline is not None:
            baseline_name = (env_sync_baseline.get("metadata") or {}).get("name")
            if (not isinstance(target_revision, str) or not target_revision.startswith(target.service_name + "-")
                    or not isinstance(baseline_name, str) or not baseline_name.startswith(target.service_name + "-")
                    or not _traffic_on_revision(service, baseline_name)
                    or service.get("status", {}).get("latestCreatedRevisionName") != target_revision):
                raise ReconcileError("env-only service changed or candidate is not current")
        if not dry_run:
            from scripts.runtime_target_continuity import verify_traffic_revision

            candidate = _run([
                "gcloud", "run", "revisions", "describe", target_revision,
                f"--project={project}", f"--region={target_region}", "--format=json",
            ], json_output=True, dry_run=False)
            candidate_commit = expected_commit
            if env_sync_baseline is not None:
                candidate_commit = (env_sync_baseline.get("metadata") or {}).get("labels", {}).get("commit-sha")
                baseline_containers = env_sync_baseline.get("spec", {}).get("containers") or []
                candidate_containers = candidate.get("spec", {}).get("containers") or []
                if len(baseline_containers) != 1 or len(candidate_containers) != 1:
                    raise ReconcileError("env-only container shape is invalid")
                if not baseline_containers[0].get("image") or baseline_containers[0].get("image") != candidate_containers[0].get("image"):
                    raise ReconcileError("env-only candidate image changed")
                if switch_only or initial_state_only:
                    from scripts.deploy_notification_only import metadata as normalized_metadata

                    template = (env_sync_service or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
                    if len(template) != 1:
                        raise ReconcileError("switch original template is invalid")
                    unnamed = not template[0].get("name")
                    normalize = _without_initial_state if initial_state_only else _without_runtime_switch
                    if normalize(env_sync_baseline.get("spec", {}), unnamed_template=unnamed) != normalize(candidate.get("spec", {}), unnamed_template=unnamed):
                        raise ReconcileError("candidate changed other service configuration")
                    if normalized_metadata(env_sync_baseline.get("metadata", {}), template=True) != normalized_metadata(candidate.get("metadata", {}), template=True):
                        raise ReconcileError("switch candidate changed service annotations or labels")
                rows = candidate_containers[0].get("env") or []
                names = [row.get("name") for row in rows]
                if len(names) != len(set(names)):
                    raise ReconcileError("env-only candidate environment is ambiguous")
                values = {row.get("name"): row.get("value") for row in rows}
                keys = ("RUNTIME_TARGET_ENABLED", "RUNTIME_TARGET_JSON", "IBKR_RECONCILIATION_EXPECTED_DIGESTS_JSON") if initial_state_only else ("RUNTIME_TARGET_ENABLED", "RUNTIME_TARGET_JSON")
                for key in keys:
                    actual_value, expected_value = values.get(key), env_sync_expected.get(key)
                    if key == "RUNTIME_TARGET_JSON" and switch_only:
                        try:
                            actual_value, expected_value = json.loads(actual_value), json.loads(expected_value)
                        except (TypeError, ValueError) as exc:
                            raise ReconcileError("switch target is invalid") from exc
                    matched = (_switch_targets_match(actual_value, expected_value,
                                disabling=env_sync_expected.get("RUNTIME_TARGET_ENABLED") == "false")
                               if key == "RUNTIME_TARGET_JSON" and switch_only else actual_value == expected_value)
                    if key not in env_sync_expected or not matched:
                        raise ReconcileError("env-only candidate target or switch changed")
            if switch_only and env_sync_expected.get("RUNTIME_TARGET_ENABLED") == "false":
                metadata = candidate.get("metadata") or {}
                if metadata.get("name") != target_revision or (metadata.get("labels") or {}).get("commit-sha") != candidate_commit:
                    raise ReconcileError("stop candidate revision identity does not match")
            else:
                verify_traffic_revision(candidate, expected_revision=target_revision, expected_commit=candidate_commit)

        if not _traffic_on_revision(service if isinstance(service, Mapping) else {}, target_revision):
            if initial_state_only:
                _adopt_initial_candidate(service, project=project, region=target_region, candidate_revision=target_revision)
            else:
                print(f"Updating {target.service_name} traffic to commit revision {target_revision}.")
                _run(
                [
                    "gcloud",
                    "run",
                    "services",
                    "update-traffic",
                    target.service_name,
                    f"--project={project}",
                    f"--region={target_region}",
                    f"--to-revisions={target_revision}=100",
                    "--quiet",
                ],
                    dry_run=dry_run,
                )
        verified = _run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                target.service_name,
                f"--project={project}",
                f"--region={target_region}",
                "--format=json",
            ],
            json_output=True,
            dry_run=dry_run,
        )
        if not _traffic_on_revision(verified if isinstance(verified, Mapping) else {}, target_revision):
            raise ReconcileError(
                f"{target.service_name} traffic is not 100% on commit revision {target_revision}"
            )
        print("initial_state_traffic_verified" if initial_state_only else f"Cloud Run traffic OK for {target.service_name}: {target_revision}")


def _legacy_jobs_for_target(platform: str, target: RuntimeTarget) -> list[str]:
    service = target.service_name
    jobs = [f"{service}-probe-scheduler", f"{service}-precheck-scheduler"]
    if service.endswith("-service"):
        base = service[: -len("-service")]
        jobs.extend([f"{base}-probe-scheduler", f"{base}-precheck-scheduler"])

    if platform == "longbridge":
        scope = service
        if scope.startswith("longbridge-quant-"):
            scope = scope[len("longbridge-quant-") :]
        if scope.endswith("-service"):
            scope = scope[: -len("-service")]
        if scope and scope != service:
            jobs.append(f"lb-{scope}-backup-execution")
    elif platform == "ibkr":
        canonical_precheck = f"{service.removesuffix('-service')}-precheck-scheduler"
        jobs.remove(canonical_precheck)
        jobs.append("interactive-brokers-monitor-dispatcher-scheduler")
        prefix = "interactive-brokers-quant-live-"
        suffix = service
        if suffix.startswith(prefix):
            suffix = suffix[len(prefix) :]
        if suffix.endswith("-service"):
            suffix = suffix[: -len("-service")]
        if suffix.startswith("u"):
            jobs.append(f"ibkr-{suffix}-backup-execution")
            jobs.append(f"ibkr-{suffix}-pre-market-dry-run")

    return list(dict.fromkeys(jobs))


def _scheduler_locations(*, region: str, scheduler_location: str, targets: Sequence[RuntimeTarget], env: Mapping[str, str]) -> list[str]:
    locations = [scheduler_location or region]
    raw_extra = str(env.get("CLOUD_SCHEDULER_LEGACY_LOCATIONS") or "").strip()
    for chunk in raw_extra.replace(";", ",").split(","):
        if chunk.strip():
            locations.append(chunk.strip())
    for target in targets:
        if target.region:
            locations.append(target.region)
    return [item for item in dict.fromkeys(locations) if item]


def delete_legacy_schedulers(
    *,
    platform: str,
    project: str,
    region: str,
    scheduler_location: str,
    targets: Sequence[RuntimeTarget],
    env: Mapping[str, str],
    dry_run: bool,
) -> None:
    locations = _scheduler_locations(
        region=region,
        scheduler_location=scheduler_location,
        targets=targets,
        env=env,
    )
    for target in targets:
        for job in _legacy_jobs_for_target(platform, target):
            for location in locations:
                if not _run_optional(
                    [
                        "gcloud",
                        "scheduler",
                        "jobs",
                        "describe",
                        job,
                        f"--project={project}",
                        f"--location={location}",
                    ],
                    dry_run=dry_run,
                ):
                    continue
                print(f"Deleting legacy Cloud Scheduler job {job} in {location}.")
                _run(
                    [
                        "gcloud",
                        "scheduler",
                        "jobs",
                        "delete",
                        job,
                        f"--project={project}",
                        f"--location={location}",
                        "--quiet",
                    ],
                    dry_run=dry_run,
                )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("ibkr", "longbridge"), required=True)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", ""))
    parser.add_argument("--region", default=os.environ.get("CLOUD_RUN_REGION", ""))
    parser.add_argument("--scheduler-location", default=os.environ.get("CLOUD_SCHEDULER_LOCATION", ""))
    parser.add_argument("--expected-commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--ensure-latest-traffic", action="store_true")
    parser.add_argument("--env-sync-evidence-dir", default="")
    parser.add_argument("--runtime-switch-only", action="store_true")
    parser.add_argument("--initial-state-only", action="store_true")
    parser.add_argument("--delete-legacy-schedulers", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.initial_state_only and (args.runtime_switch_only or not args.env_sync_evidence_dir):
        raise ReconcileError("initial state requires exclusive exact revision evidence")
    if not args.project:
        raise ReconcileError("--project or GCP_PROJECT_ID is required")
    targets = load_targets(env=os.environ)
    if not targets:
        raise ReconcileError("No Cloud Run targets resolved from SYNC_PLAN_JSON, CLOUD_RUN_SERVICE_TARGETS_JSON, or CLOUD_RUN_SERVICE")
    if args.ensure_latest_traffic:
        extra = {}
        if args.env_sync_evidence_dir:
            from pathlib import Path
            directory = Path(args.env_sync_evidence_dir)
            baseline = json.loads((directory / "baseline-revision.json").read_text(encoding="utf-8"))
            service = json.loads((directory / "baseline-service.json").read_text(encoding="utf-8"))
            revision = (directory / "candidate-revision.txt").read_text(encoding="utf-8").strip()
            plan = _load_json_object(os.environ.get("SYNC_PLAN_JSON", ""), field_name="SYNC_PLAN_JSON")
            entries = plan.get("targets") if isinstance(plan, Mapping) else None
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], Mapping):
                raise ReconcileError("env-only sync requires a single target plan")
            extra = {"env_sync_baseline": baseline, "env_sync_revision": revision,
                     "env_sync_expected": entries[0].get("env"), "switch_only": args.runtime_switch_only,
                     "initial_state_only": args.initial_state_only,
                     "env_sync_service": service}
        elif args.runtime_switch_only or args.initial_state_only:
            raise ReconcileError("runtime switch requires exact revision evidence")
        ensure_latest_traffic(
            project=args.project,
            region=args.region,
            targets=targets,
            expected_commit=args.expected_commit,
            dry_run=args.dry_run,
            **extra,
        )
    if args.delete_legacy_schedulers:
        delete_legacy_schedulers(
            platform=args.platform,
            project=args.project,
            region=args.region,
            scheduler_location=args.scheduler_location,
            targets=targets,
            env=os.environ,
            dry_run=args.dry_run,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReconcileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
