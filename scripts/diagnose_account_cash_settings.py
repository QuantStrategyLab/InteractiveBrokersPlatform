#!/usr/bin/env python3
"""Read scoped serving-revision cash declarations; no reports, secrets or apply.

Only fixed field states, comparisons and revision hashes leave this diagnostic. Deployed environment
declarations do not attest in-process consumption, spendability or risk policy.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import subprocess
import sys

from scripts import diagnose_account_facts_identity as identity
from scripts import diagnose_serving_image_identity as image_identity

SCHEMA = "ibkr.serving_cash_settings_diagnostic.v1"
RATIO = "IBKR_RESERVED_CASH_RATIO"
OVERRIDE_RATIO = "QSL_IBKR_RESERVED_CASH_RATIO"
FLOOR = "IBKR_MIN_RESERVED_CASH_USD"
OVERRIDE_FLOOR = "QSL_IBKR_MIN_RESERVED_CASH_USD"
CASH_FIELDS = (RATIO, OVERRIDE_RATIO, FLOOR, OVERRIDE_FLOOR)
REASONS = frozenset({"expected_target_invalid", "cloud_region_unavailable", "metadata_read_failed",
    "metadata_invalid", "serving_traffic_unresolved", "revision_identity_invalid",
    "deployed_target_mismatch", "serving_changed_during_read", "cash_declarations_invalid",
    "cash_declarations_observed", "diagnostic_error", "arguments_invalid"})


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def _output():
    return dict(schema_version=SCHEMA, status="UNAVAILABLE", reason="diagnostic_error",
        observed_at=None, serving_stable=None, scope_verified=False,
        serving_revision_sha256=None, fields={}, selected_ratio_field=None, selected_floor_field=None,
        intent_comparisons={},
        target_source_statuses={}, scope_equalities={},
        source_classification="SERVING_REVISION_ENV_DECLARATIONS_ONLY",
        missing_fields_defaulted=False, runtime_consumption_verified=False,
        spendable_cash_verified=False, account_policy_bound=False,
        settings_applied=False, execution_authorized=False, breaker_reset=False)


def _row(rows, name):
    found = [row for row in rows if row.get("name") == name]
    if not found:
        return "ABSENT", None
    if len(found) != 1:
        return "DUPLICATE", None
    row = found[0]
    if "valueFrom" in row:
        return "SECRET_REFERENCE" if "value" not in row else "INVALID", None
    value = row.get("value")
    if not isinstance(value, str):
        return "INVALID", None
    return ("BLANK", value) if not value.strip() else ("EXPLICIT", value)


def project_cash_fields(rows):
    """No defaults, numbers or brute-forceable numeric hashes are disclosed."""
    _require(isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows), "metadata_invalid")
    result = {}
    for name in CASH_FIELDS:
        state, raw = _row(rows, name)
        zero = None
        if state == "EXPLICIT":
            try:
                number = float(raw.strip())
                _require(math.isfinite(number) and number >= 0
                    and (name in (FLOOR, OVERRIDE_FLOOR) or number <= 1), "cash_declarations_invalid")
                # Match the existing environment parser's float representation.
                zero = number == 0
            except (ValueError, OverflowError):
                state = "INVALID"
        result[name] = dict(status=state, explicit_zero=zero)
    # Match ``getenv(QSL_...) or getenv(...)``: empty falls through, but a
    # whitespace-only override is selected and then resolves to no ratio.
    selected = {}
    for kind, base, override in (("ratio", RATIO, OVERRIDE_RATIO), ("floor", FLOOR, OVERRIDE_FLOOR)):
        override_state, override_raw = _row(rows, override)
        selected[kind] = base if override_state == "ABSENT" or override_raw == "" else override
    return result, selected


def compare_cash_fields(rows, reference):
    """Compare in memory; never publish a low-entropy amount fingerprint."""
    _require(isinstance(reference, Mapping) and set(reference) <= set(CASH_FIELDS)
        and all(isinstance(value, str) for value in reference.values()), "cash_declarations_invalid")
    observed, _ = project_cash_fields(rows)
    ref_rows = [dict(name=name, value=value) for name, value in reference.items()]
    wanted, _ = project_cash_fields(ref_rows)
    return {name: dict(intent_status=wanted[name]["status"], observed_status=observed[name]["status"],
        same_explicit_number=(float(_row(rows, name)[1]) == float(reference[name])
            if wanted[name]["status"] == observed[name]["status"] == "EXPLICIT" else None))
        for name in CASH_FIELDS}


def _read(command):
    try:
        result = subprocess.run(tuple(command), capture_output=True, text=True, check=False, timeout=30,
            env={**os.environ, "CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "true", "CLOUDSDK_CORE_LOG_HTTP": "false"})
        _require(result.returncode == 0, "metadata_read_failed")
        value = json.loads(result.stdout)
        _require(isinstance(value, Mapping), "metadata_invalid")
        return value
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("metadata_read_failed") from exc
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("metadata_invalid") from exc


def _service(expected, region):
    value = _read(("gcloud", "run", "services", "describe", expected["expected_service_name"],
        "--project", expected["expected_project_id"], "--region", region, "--format=json"))
    _require(isinstance(value.get("metadata"), Mapping)
        and value["metadata"].get("name") == expected["expected_service_name"]
        and isinstance(value.get("status"), Mapping), "metadata_invalid")
    return value


def _json_target(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "deployed_target_mismatch")
            value[key] = item
        return value
    try:
        result = json.loads(raw, object_pairs_hook=unique)
        _require(isinstance(result, Mapping), "deployed_target_mismatch")
        return result
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("deployed_target_mismatch") from exc


def diagnose_cash_settings(expected, *, strategy_profile, region, now=None, expected_declarations=None):
    """Read service → exact serving revision → stable service, all metadata only."""
    out = _output()
    try:
        _require(isinstance(expected, Mapping) and all(isinstance(expected.get(key), str)
            and bool(expected[key].strip()) and expected[key] == expected[key].strip()
            and "\n" not in expected[key] and "\r" not in expected[key] for key in (
                "expected_project_id", "expected_service_name", "expected_account_scope",
                "expected_deployment_selector")), "expected_target_invalid")
        _require(len(identity.publisher._selector(expected.get("expected_account_selector"))) == 1
            and isinstance(strategy_profile, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,119}", strategy_profile), "expected_target_invalid")
        expected = dict(expected, expected_runtime_revision=expected.get("expected_runtime_revision"),
            expected_account_selector=identity.publisher._selector(expected["expected_account_selector"]))
        _require(isinstance(region, str) and re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region),
            "cloud_region_unavailable")
        current = datetime.now(timezone.utc) if now is None else now
        _require(isinstance(current, datetime) and current.utcoffset() is not None, "metadata_invalid")
        out["observed_at"] = current.astimezone(timezone.utc).isoformat()
        service = _service(expected, region)
        serving, _ = image_identity._serving(service, expected)
        revision = _read(("gcloud", "run", "revisions", "describe", serving,
            "--project", expected["expected_project_id"], "--region", region, "--format=json"))
        _require(isinstance(revision.get("metadata"), Mapping)
            and revision["metadata"].get("name") == serving, "revision_identity_invalid")
        spec = revision.get("spec")
        containers = spec.get("containers") if isinstance(spec, Mapping) else None
        _require(isinstance(containers, list) and len(containers) == 1
            and isinstance(containers[0], Mapping), "metadata_invalid")
        rows = containers[0].get("env")
        _require(isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows), "metadata_invalid")
        target_state, raw_target = _row(rows, "RUNTIME_TARGET_JSON")
        override_state, raw_override = _row(rows, "QSL_RUNTIME_TARGET_JSON")
        out["target_source_statuses"] = dict(RUNTIME_TARGET_JSON=target_state, QSL_RUNTIME_TARGET_JSON=override_state)
        use_override = override_state != "ABSENT" and raw_override != ""
        _require((override_state if use_override else target_state) == "EXPLICIT", "deployed_target_mismatch")
        target = _json_target(raw_override if use_override else raw_target)
        # Original target and risk-policy loaders use opposite alias priority.
        # When both are supplied, they must agree; do not guess which identity
        # to trust or treat an unread secret as the same target.
        if use_override and target_state != "ABSENT" and raw_target != "":
            _require(target_state == "EXPLICIT" and _json_target(raw_target) == target,
                "deployed_target_mismatch")
        out["scope_equalities"] = dict(identity._source_scope(target, expected),
            platform_id=target.get("platform_id") == "ibkr",
            strategy_profile=target.get("strategy_profile") == strategy_profile)
        _require(all(value is True for value in out["scope_equalities"].values()), "deployed_target_mismatch")
        fields, selected = project_cash_fields(rows)
        comparisons = compare_cash_fields(rows, expected_declarations) if expected_declarations is not None else {}
        after, _ = image_identity._serving(_service(expected, region), expected)
        out["serving_stable"] = after == serving
        _require(out["serving_stable"], "serving_changed_during_read")
        _require(not any(row["status"] in {"INVALID", "DUPLICATE"} for row in fields.values()),
            "cash_declarations_invalid")
        out.update(status="OBSERVED", reason="cash_declarations_observed", scope_verified=True,
            serving_revision_sha256=_digest(serving), fields=fields,
            selected_ratio_field=selected["ratio"], selected_floor_field=selected["floor"],
            intent_comparisons=comparisons)
    except Exception as exc:
        reason = str(exc)
        out["reason"] = reason if reason in REASONS else "diagnostic_error"
        # Do not publish even partial values/hashes after an identity or read failure.
        out.update(fields={}, intent_comparisons={}, scope_verified=False)
    return out


def main(argv: Sequence[str] | None = None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--strategy-profile":
        out = _output(); out["reason"] = "arguments_invalid"
    else:
        try:
            out = diagnose_cash_settings(identity.resolve_expected(), strategy_profile=args[1],
                region=os.environ.get("CLOUD_RUN_REGION", ""))
        except Exception:
            out = _output(); out["reason"] = "expected_target_invalid"
    print(json.dumps(out, sort_keys=True))
    return 0 if out["status"] == "OBSERVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
