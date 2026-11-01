"""Offline consistency checks for captured, already sanitized IBKR source fields.

This module performs no I/O and imports no application or runtime package. Source
locators, raw digests, aliases and producer revisions remain unauthenticated
declarations. A matching projection can never establish historical parity or
authorize execution. The account-binding schema belongs to this intake contract;
it is not a claim that the runtime emitted a physical-account-domain record.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re

SCHEMA = "qsl.ibkr_source_evidence.v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_DOMAIN = {"platform", "execution_environment", "server", "lifecycle_id", "account_alias"}
_RELEASE_HASHES = ("manifest_sha256", "config_sha256", "risk_policy_sha256", "evidence_sha256", "plugin_bundle_sha256")
_RELEASE_FIELDS = {"release_id", "strategy_revision", "effective_session", *_RELEASE_HASHES}
_ACCOUNT_PATHS = ("/runtime_target/account_selector/0", "/summary/account_ids/0",
                  "/summary/account_facts/account_ids/0")
_REPORT_FILES = {
    "QuantPlatformKit:src/quant_platform_kit/common/runtime_reports.py": "7f5b33f84810d414cc665ce33d2741497b8f54fcb300488c51f54dbd08bdcf42",
    "QuantPlatformKit:src/quant_platform_kit/common/execution_receipts.py": "5b7f5b44c8aa4bda640e72cc5a58f04b617404afcf9f55ceb314a1721be6d93b",
    "QuantPlatformKit:src/quant_platform_kit/common/strategy_release.py": "773547422aa537eb03bf223a4d9038db457c514211b0387e4601afdcef9727b9",
    "QuantPlatformKit:src/quant_platform_kit/common/runtime_target.py": "17fa7505e1c8e6e375699d44120526c8c9af8c586a274fd7d7dd03009131c5b3",
    "InteractiveBrokersPlatform:application/runtime_reporting_adapters.py": "d2394847474ef78c98cc51c3970fd539481bf13d60ac5cc941fd03d33850dace",
    "InteractiveBrokersPlatform:application/execution_receipt_adapter.py": "a2bd939380947374854cf373ef27c7266c7b97150f0742e6b90191d7e5a278c0",
    "InteractiveBrokersPlatform:application/account_facts.py": "e4513e5ecc8f494c3a305007ebae21804b07ac1407d761c1af3c2ce9d53af4d1",
    "InteractiveBrokersPlatform:application/decision_cycle.py": "ad938a55aa439764112e96ac7e80a4c661ee14823292e7cf99f1df2bcc863311",
    "InteractiveBrokersPlatform:main.py": "f13b55f501c404ef249bb3344bd69136beb8e0ead382e827d082217180b46637",
}
_DAILY_FILES = {
    "InteractiveBrokersPlatform:scripts/project_ibkr_runtime_daily.py": "d5cb50eabc838c9c47064c746f3f15dbdb427e17c95ce1a70f7ff0ee580afb03",
}
_FIELD_SOURCES = {
    "profile": "source.payload#/strategy_profile", "run_id": "source.payload#/run_id",
    "logical_account_scope": "source.payload#/account_scope",
    "runtime_revision": "source.payload#/diagnostics/runtime_revision",
    "release": "source.payload#/runtime_release_receipt/strategy_release",
    "started_at": "source.payload#/started_at", "finished_at": "source.payload#/finished_at",
    "decision_at": "source.payload#/decision_at",
    "account_domain": "account_binding.payload#/account_domain",
    "receipt": "source.payload#/execution_receipt",
    "execution_lane": ["source.payload#/dry_run", "source.payload#/runtime_target/execution_environment",
                       "source.payload#/runtime_target/execution_mode", "source.payload#/diagnostics/ib_gateway_mode"],
}
_CONFIRMATIONS = {
    "not_due": {"not_applicable"}, "no_action": {"not_applicable"}, "no_signal": {"not_applicable"},
    "no_rebalance": {"not_applicable"}, "risk_blocked": {"not_applicable"},
    "submitted": {"not_observed"}, "broker_acknowledged": {"acknowledged"},
    "partially_filled": {"partially_filled"}, "filled": {"filled"},
    "reconciliation_required": {"reconciliation_required"},
    "failed": {"not_applicable", "not_observed", "reconciliation_required"},
}
_GAPS = (
    "SOURCE_AUTHENTICATION_NOT_ESTABLISHED", "ORIGINAL_BYTES_NOT_RECOMPUTED",
    "ALIAS_TO_ORIGINAL_BINDING_UNVERIFIED", "PHYSICAL_ACCOUNT_DOMAIN_UNAUTHENTICATED",
    "PRODUCER_REVISION_AND_DEPLOYMENT_UNVERIFIED", "SOURCE_AVAILABILITY_UNVERIFIED",
    "CLOCK_SOURCE_ASSOCIATION_UNVERIFIED",
    "CALENDAR_AND_ARCHIVE_COMPLETENESS_UNVERIFIED", "RUN_PURPOSE_NOT_INDEPENDENTLY_ATTESTED",
    "RECEIPT_HAS_NO_NATIVE_RUN_ID", "INDIVIDUAL_FILLS_AND_PENDING_ACCOUNT_STATE_UNVERIFIED",
    "CROSS_RUN_STRATEGY_STATE_CONTINUITY_UNVERIFIED", "DECISION_TO_ORDER_CAUSALITY_UNVERIFIED",
    "PLUGIN_PAYLOAD_AND_PUBLICATION_PROVENANCE_UNVERIFIED", "SHARED_ACCOUNT_ACCEPTANCE_UNVERIFIED",
)


class _Issue(ValueError):
    def __init__(self, code, *, blocked=False):
        super().__init__(code)
        self.code = code
        self.status = "BLOCKED" if blocked else "FAIL"


def _require(condition, code, *, blocked=False):
    if not condition:
        raise _Issue(code, blocked=blocked)


def _object(value):
    _require(value is not None, "REQUIRED_OBSERVATION_MISSING", blocked=True)
    _require(type(value) is dict, "OBJECT_REQUIRED")
    return value


def _need(value, key):
    value = _object(value)
    _require(key in value and value[key] is not None, "REQUIRED_OBSERVATION_MISSING", blocked=True)
    return value[key]


def _peek(value, key):
    return value.get(key) if type(value) is dict else None


def _identifier(value):
    _require(type(value) is str and _ID.fullmatch(value) is not None, "BOUNDED_IDENTIFIER_REQUIRED")
    return value


def _digest(value):
    _require(type(value) is str and _HASH.fullmatch(value) is not None, "SHA256_REQUIRED")
    return value


def _json_shape(value, depth=0):
    _require(depth <= 64, "JSON_DEPTH_EXCEEDED")
    if type(value) is dict:
        _require(all(type(key) is str for key in value), "JSON_STRING_KEYS_REQUIRED")
        for item in value.values():
            _json_shape(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_shape(item, depth + 1)
    else:
        _require(value is None or type(value) in {str, int, float, bool}, "JSON_VALUE_REQUIRED")


def _canonical_sha256(value):
    _json_shape(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    _require(len(encoded) <= 2_000_000, "OBSERVATION_SIZE_EXCEEDED")
    return hashlib.sha256(encoded).hexdigest()


def _clock(value):
    _require(type(value) is str and len(value) <= 64, "UTC_TIMESTAMP_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _Issue("UTC_TIMESTAMP_REQUIRED") from error
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "UTC_TIMESTAMP_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _path(value, pointer):
    for part in pointer.lstrip("/").split("/"):
        if type(value) is list:
            _require(part.isdecimal() and int(part) < len(value), "REQUIRED_OBSERVATION_MISSING", blocked=True)
            value = value[int(part)]
        else:
            value = _need(value, part)
    return value


def _envelope(value):
    _identifier(_need(value, "source_id"))
    _require(_need(value, "digest_domain") == "sanitized_json", "DIGEST_DOMAIN_MISMATCH")
    locator = _need(value, "locator")
    alias = _need(locator, "object_alias")
    _require(type(alias) is str and re.fullmatch(r"obj_[0-9a-f]{16,64}", alias), "OBJECT_ALIAS_REQUIRED")
    _identifier(_need(locator, "version"))
    _digest(_need(locator, "raw_bytes_sha256"))
    _clock(_need(value, "captured_at"))
    payload = _object(_need(value, "payload"))
    _require(_canonical_sha256(payload) == _digest(_need(value, "payload_sha256")), "PAYLOAD_DIGEST_MISMATCH")


def _producer(source, reviewed):
    producer = _need(source, "producer")
    for field in ("platform_revision", "qpk_revision"):
        value = _need(producer, field)
        _require(type(value) is str and _REVISION.fullmatch(value), "SOURCE_REVISION_REQUIRED")
    files = _need(producer, "files")
    for path, expected in reviewed.items():
        _require(_digest(_need(files, path)) == expected, "PRODUCER_VERSION_UNSUPPORTED", blocked=True)


def _header(document):
    _require(_need(document, "schema_version") == SCHEMA, "UNSUPPORTED_SCHEMA", blocked=True)
    _require(_need(document, "evidence_kind") in {"historical", "synthetic"}, "EVIDENCE_KIND_REQUIRED")


def _source_identity(report, normalized):
    _require(_need(report, "schema_version") == "runtime_report.v1", "SOURCE_SCHEMA_UNSUPPORTED", blocked=True)
    _require(_need(report, "platform") == "interactive_brokers" and _need(normalized, "platform") == "ibkr",
             "PLATFORM_IDENTITY_MISMATCH")
    profile = _need(report, "strategy_profile")
    _require(profile in {"tqqq_growth_income", "soxl_soxx_trend_income"}, "PROFILE_UNSUPPORTED", blocked=True)
    target = _need(report, "runtime_target")
    _require(_need(target, "strategy_profile") == profile, "PROFILE_IDENTITY_MISMATCH")
    for key, pointer in _FIELD_SOURCES.items():
        if key in {"release", "account_domain", "receipt", "execution_lane"}:
            continue
        actual = _path(report, pointer.split("#", 1)[1])
        _require(_need(normalized, key) == actual, "NORMALIZATION_MISMATCH")
    _require(_need(target, "account_scope") == _need(report, "account_scope"), "LOGICAL_SCOPE_MISMATCH")
    _identifier(_need(report, "account_scope"))
    _require(re.fullmatch(r"run_[0-9a-f]{16,64}", _identifier(_need(report, "run_id"))), "RUN_ALIAS_REQUIRED")


def _normalization(document, source, binding, normalized):
    _require(_canonical_sha256(_object(normalized)) == _digest(_need(document, "normalized_sha256")),
             "NORMALIZED_DIGEST_MISMATCH")
    _require(_need(normalized, "source_id") == _need(source, "source_id")
             and _need(normalized, "source_payload_sha256") == _need(source, "payload_sha256")
             and _need(normalized, "account_binding_payload_sha256") == _need(binding, "payload_sha256"),
             "SOURCE_REFERENCE_MISMATCH")
    _require(_need(normalized, "field_sources") == _FIELD_SOURCES, "SOURCE_FIELD_MAPPING_MISMATCH")
    for key in ("individual_fills", "pending_orders"):
        _require(_need(normalized, key) == "UNKNOWN", "UNSUPPORTED_EVIDENCE_PROMOTION")


def _source_locations(document):
    names = ["source", "account_binding"]
    if "daily_projection" in _object(document):
        names.append("daily_projection")
    identities, locations = set(), set()
    for name in names:
        source = _need(document, name)
        identity = _identifier(_need(source, "source_id"))
        _require(identity not in identities, "DUPLICATE_SOURCE_ID")
        identities.add(identity)
        locator = _need(source, "locator")
        location = (_identifier(_need(locator, "object_alias")), _identifier(_need(locator, "version")))
        _require(location not in locations, "SOURCE_LOCATOR_REUSED")
        locations.add(location)


def _account_binding(source, binding, report, normalized):
    payload = _need(binding, "payload")
    _require(_need(payload, "schema_version") == "qsl.sanitized_account_binding.v1",
             "ACCOUNT_BINDING_SCHEMA_UNSUPPORTED", blocked=True)
    _require(_need(payload, "report_source_id") == _need(source, "source_id")
             and _need(payload, "report_payload_sha256") == _need(source, "payload_sha256"),
             "SOURCE_REFERENCE_MISMATCH")
    _require(_need(binding, "source_id") != _need(source, "source_id"), "DUPLICATE_SOURCE_ID")
    domain = _object(_need(payload, "account_domain"))
    _require(set(domain) == _DOMAIN, "ACCOUNT_DOMAIN_FIELDS_REQUIRED", blocked=True)
    for value in domain.values():
        _identifier(value)
    _require(domain["platform"] == "ibkr", "PLATFORM_IDENTITY_MISMATCH")
    _require(domain["execution_environment"] in {"paper", "live"}, "ACCOUNT_ENVIRONMENT_UNKNOWN", blocked=True)
    _require(re.fullmatch(r"acct_[0-9a-f]{16,64}", domain["account_alias"]), "ACCOUNT_ALIAS_REQUIRED")
    _require(_need(normalized, "account_domain") == domain, "ACCOUNT_DOMAIN_MISMATCH")
    _require(_path(report, "/summary/account_facts/schema_version") == "ibkr_account_snapshot.v1",
             "ACCOUNT_SOURCE_SCHEMA_UNSUPPORTED", blocked=True)
    for path in _ACCOUNT_PATHS:
        accounts = _path(report, path.rsplit("/", 1)[0])
        _require(type(accounts) is list, "ACCOUNT_ARRAY_REQUIRED")
        _require(len(accounts) == 1, "SINGLE_ACCOUNT_OBSERVATION_REQUIRED", blocked=True)
        _require(accounts[0] == domain["account_alias"], "ACCOUNT_IDENTITY_MISMATCH")
    first, last = (_clock(_need(payload, key)) for key in ("effective_from", "effective_until"))
    _require(first < last and first <= _clock(_need(report, "decision_at")) < last, "ACCOUNT_BINDING_NOT_EFFECTIVE")
    # A supplied physical-server alias is not inferred from Gateway host/address.
    expected_namespace = ":".join(domain[key] for key in ("platform", "execution_environment", "server", "lifecycle_id"))
    _require(_need(_need(source, "sanitization"), "namespace") == expected_namespace, "ALIAS_NAMESPACE_MISMATCH")


def _aliases(source, report):
    redaction = _need(source, "sanitization")
    _require(_need(redaction, "schema_version") == "qsl.ibkr_source_aliases.v1", "ALIAS_SCHEMA_UNSUPPORTED", blocked=True)
    rows = _need(redaction, "substitutions")
    _require(type(rows) is list and len(rows) <= 1000, "ALIAS_ARRAY_REQUIRED")
    seen, originals, aliases = set(), {}, {}
    for row in rows:
        pointer = _need(row, "pointer")
        _require(type(pointer) is str and pointer.startswith("/") and len(pointer) <= 256, "SOURCE_POINTER_REQUIRED")
        _require(pointer not in seen, "DUPLICATE_ALIAS_POINTER")
        seen.add(pointer)
        alias = _identifier(_need(row, "alias"))
        original = _digest(_need(row, "original_value_sha256"))
        _require(_path(report, pointer) == alias, "ALIAS_FIELD_MISMATCH")
        _require(originals.get(original, alias) == alias and aliases.get(alias, original) == original,
                 "ALIAS_MAPPING_CONFLICT")
        originals[original], aliases[alias] = alias, original
    _require({"/run_id", *_ACCOUNT_PATHS} <= seen, "REQUIRED_ALIAS_MAPPING_MISSING", blocked=True)


def _release(report, normalized):
    loaded = _need(report, "runtime_release_receipt")
    _require(_need(loaded, "schema_version") == "runtime_loaded_receipt.v1", "RELEASE_SCHEMA_UNSUPPORTED", blocked=True)
    _require(_need(loaded, "attestation_state") == "self_attested", "RELEASE_ATTESTATION_UNAVAILABLE", blocked=True)
    release = _object(_need(loaded, "strategy_release"))
    _require(set(release) == _RELEASE_FIELDS, "RELEASE_FIELDS_INCOMPLETE", blocked=True)
    _identifier(_need(release, "release_id"))
    revision = _need(release, "strategy_revision")
    _require(type(revision) is str and _REVISION.fullmatch(revision), "SOURCE_REVISION_REQUIRED")
    for field in _RELEASE_HASHES:
        _digest(_need(release, field))
    session = _need(release, "effective_session")
    _require(type(session) is str and date.fromisoformat(session).isoformat() == session, "RELEASE_SESSION_REQUIRED")
    _require(_need(loaded, "release_id") == release["release_id"]
             and _need(_need(report, "runtime_target"), "strategy_release") == release
             and _need(normalized, "release") == release, "RELEASE_IDENTITY_MISMATCH")
    _require(_need(loaded, "runtime_revision") == _path(report, "/diagnostics/runtime_revision"), "RUNTIME_REVISION_MISMATCH")


def _mode(report, binding, normalized):
    target, diagnostics = _need(report, "runtime_target"), _need(report, "diagnostics")
    mode = _need(target, "execution_mode")
    _require(mode in {"paper", "live"}, "EXECUTION_ENVIRONMENT_UNKNOWN", blocked=True)
    environment = _need(target, "execution_environment")
    _require(environment in {"paper", "live", "dry_run"}, "EXECUTION_ENVIRONMENT_UNKNOWN", blocked=True)
    gateway = _need(diagnostics, "ib_gateway_mode")
    _require(gateway in {"paper", "live"}, "GATEWAY_ENVIRONMENT_UNKNOWN", blocked=True)
    domain = _path(binding, "/payload/account_domain")
    _require(_need(domain, "execution_environment") == gateway, "EXECUTION_ENVIRONMENT_MISMATCH")
    if environment in {"paper", "live"}:
        _require(environment == gateway, "EXECUTION_ENVIRONMENT_MISMATCH")
    dry = _need(report, "dry_run")
    _require(type(dry) is bool, "DRY_RUN_BOOLEAN_REQUIRED")
    target_dry = _need(target, "dry_run_only")
    _require(type(target_dry) is bool, "DRY_RUN_BOOLEAN_REQUIRED")
    _require(not target_dry or dry, "DRY_RUN_CONFIGURATION_CONFLICT")
    # This reviewed producer retains a legacy mode property derived only from
    # dry_run_only. Its "paper" label is not a physical-account observation.
    _require(mode == ("paper" if target_dry else "live"), "LEGACY_MODE_CONFLICT")
    _require((environment == "dry_run") == target_dry, "DRY_RUN_CONFIGURATION_CONFLICT")
    _require(_need(normalized, "execution_lane") == ("dry_run" if dry else environment), "EXECUTION_LANE_MISMATCH")


def _record_kind(report, normalized):
    diagnostics, summary = _object(_need(report, "diagnostics")), _object(_need(report, "summary"))
    for field in ("maintenance_cash_observation", "validation_only"):
        if field in diagnostics:
            _require(type(diagnostics[field]) is bool, "DIAGNOSTIC_FLAG_INVALID")
    kind = "runtime_report"
    if diagnostics.get("maintenance_cash_observation") is True:
        kind = "maintenance_account_observation"
    elif diagnostics.get("validation_only") is True or diagnostics.get("mode") in {"diagnostic", "validation"}:
        kind = "diagnostic"
    elif "paper_execution_command_consumer" in summary:
        _object(summary["paper_execution_command_consumer"])
        kind = "paper_command_reconciliation"
    _require(_need(normalized, "record_kind") == kind, "RECORD_KIND_MISMATCH")


def _clocks(source, report):
    start, end, decision = (_clock(_need(report, key)) for key in ("started_at", "finished_at", "decision_at"))
    # cycle_handler establishes the whole-second clock before constructing the
    # report. Initialization can span seconds; no lower delay bound is known.
    _require(decision.microsecond == 0 and decision <= start <= end <= _clock(_need(source, "captured_at")),
             "RUN_CLOCK_CONFLICT")
    loaded = _clock(_path(report, "/runtime_release_receipt/loaded_at"))
    _require(loaded == start, "RELEASE_CLOCK_CONFLICT")
    account = _clock(_path(report, "/summary/account_facts/observed_at"))
    _require(start <= account <= end, "ACCOUNT_CLOCK_CONFLICT")


def _receipt(source, report, normalized):
    receipt = _object(_need(report, "execution_receipt"))
    fields = {"schema_version", "receipt_id", "platform", "strategy_profile", "strategy_revision",
              "execution_mode", "outcome", "broker_confirmation", "observed_at"}
    _require(set(receipt) <= fields, "RECEIPT_NATIVE_FIELDS_CONFLICT")
    _require(set(receipt) == fields, "RECEIPT_FIELDS_MISSING", blocked=True)
    _require(receipt["schema_version"] == "qsl_execution_receipt.v1", "RECEIPT_SCHEMA_UNSUPPORTED", blocked=True)
    outcome, confirmation = receipt["outcome"], receipt["broker_confirmation"]
    _require(type(outcome) is str and outcome in _CONFIRMATIONS, "RECEIPT_OUTCOME_UNSUPPORTED", blocked=True)
    _require(type(confirmation) is str and confirmation in _CONFIRMATIONS[outcome], "RECEIPT_OUTCOME_CONFLICT")
    public = {key: value for key, value in receipt.items() if key != "receipt_id"}
    _require(receipt["receipt_id"] == "execution-receipt." + _canonical_sha256(public)[:32], "RECEIPT_DIGEST_MISMATCH")
    release = _path(report, "/runtime_release_receipt/strategy_release")
    _require(receipt["platform"] == "ibkr" and receipt["strategy_profile"] == _need(report, "strategy_profile")
             and receipt["strategy_revision"] == _need(release, "strategy_revision"), "RECEIPT_IDENTITY_MISMATCH")
    mode = _path(report, "/runtime_target/execution_mode")
    _require(mode in {"paper", "live"}, "EXECUTION_ENVIRONMENT_UNKNOWN", blocked=True)
    _require(receipt["execution_mode"] == mode, "RECEIPT_IDENTITY_MISMATCH")
    _require(_need(normalized, "receipt") == receipt, "RECEIPT_NORMALIZATION_MISMATCH")
    at = _clock(receipt["observed_at"])
    _require(receipt["observed_at"] == at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
             "RECEIPT_CLOCK_NOT_CANONICAL")
    _require(at.microsecond == 0 and _clock(_need(report, "started_at")).replace(microsecond=0)
             <= at <= _clock(_need(source, "captured_at")), "RECEIPT_CLOCK_CONFLICT")
    if _need(report, "dry_run") is True:
        _require(outcome in {"no_action", "failed"} and confirmation == "not_applicable", "DRY_RUN_RECEIPT_CONFLICT")
    execution = _peek(_peek(report, "summary"), "execution_status")
    if execution in {"pending_reconciliation", "reconciliation_required", "unknown", "unknown_order"}:
        _require(outcome == "reconciliation_required" or
                 (outcome == "failed" and confirmation in {"not_observed", "reconciliation_required"}),
                 "CYCLE_RECEIPT_OUTCOME_CONFLICT")
    # The finally path may create a terminal receipt after report finalization.
    # Native receipt fields do not identify that path: retain the observation
    # without claiming its clock association has been established.
    _require(at <= _clock(_need(report, "finished_at")), "POST_FINISH_RECEIPT_SOURCE_UNVERIFIED", blocked=True)


def _daily(source, report, normalized):
    _envelope(source)
    _producer(source, _DAILY_FILES)
    payload = _need(source, "payload")
    _require(_need(payload, "platform") == "ibkr", "PLATFORM_IDENTITY_MISMATCH")
    _require(_clock(_need(payload, "observed_at")) <= _clock(_need(source, "captured_at")), "DAILY_CLOCK_CONFLICT")
    _require(_need(payload, "completeness") == "incomplete", "DAILY_COVERAGE_CONFLICT")
    errors = _need(payload, "read_errors")
    _require(type(errors) is list and "coverage_unconfirmed" in errors, "DAILY_COVERAGE_CONFLICT")
    records = _need(payload, "records")
    _require(type(records) is list and len(records) == 1, "DAILY_TARGET_SCOPE_UNSUPPORTED", blocked=True)
    record = _object(records[0])
    _require(_need(record, "completeness") == "incomplete", "DAILY_COVERAGE_CONFLICT")
    target = _need(record, "target")
    for field, report_field in (("service", "service_name"), ("strategy_profile", "strategy_profile"), ("account_scope", "account_scope")):
        _require(_need(target, field) == _need(report, report_field), "DAILY_IDENTITY_MISMATCH")
    runs = _need(record, "runs")
    _require(type(runs) is list and len(runs) <= 20, "DAILY_REPORT_LIMIT_CONFLICT")
    fills = _object(_need(record, "fills"))
    _require(set(fills) == {"source", "records", "count"}, "DAILY_FILL_FIELDS_MISSING", blocked=True)
    _require(fills == {"source": "not_connected", "records": [], "count": None}, "DAILY_FILL_EVIDENCE_CONFLICT")
    projection = _need(normalized, "daily_projection")
    _require(_need(projection, "source_id") == _need(source, "source_id")
             and _need(projection, "source_payload_sha256") == _need(source, "payload_sha256"), "SOURCE_REFERENCE_MISMATCH")
    _require(_need(projection, "coverage") == "UNKNOWN" and _need(projection, "individual_fills") == "UNKNOWN",
             "UNSUPPORTED_EVIDENCE_PROMOTION")


def validate_source_evidence(document) -> dict:
    """Return redacted local findings with permanently denied execution authority.

    Missing or unsupported evidence is BLOCKED. A checked contradiction is FAIL;
    an unsupported producer does not hide contradictions in independent groups.
    CONSISTENT describes only the supplied, sanitized observations and fixed
    source-field mappings. The outer status is never PASS.
    """
    findings, checked = [], []

    def check(scope, function, *args):
        try:
            function(*args)
            checked.append(scope)
        except _Issue as error:
            findings.append({"scope": scope, "status": error.status, "code": error.code})
        except (TypeError, ValueError, OverflowError, RecursionError):
            findings.append({"scope": scope, "status": "FAIL", "code": "MALFORMED_JSON_EVIDENCE"})

    check("document", _header, document)
    source, binding, normalized = (_peek(document, key) for key in ("source", "account_binding", "normalized"))
    report = _peek(source, "payload")
    check("source_envelope", _envelope, source)
    check("account_binding_envelope", _envelope, binding)
    check("source_locations", _source_locations, document)
    check("producer", _producer, source, _REPORT_FILES)
    check("normalization", _normalization, document, source, binding, normalized)
    check("source_identity", _source_identity, report, normalized)
    check("account_binding", _account_binding, source, binding, report, normalized)
    check("aliases", _aliases, source, report)
    check("release", _release, report, normalized)
    check("mode", _mode, report, binding, normalized)
    check("record_kind", _record_kind, report, normalized)
    check("clocks", _clocks, source, report)
    check("receipt", _receipt, source, report, normalized)
    gaps = list(_GAPS)
    if type(document) is dict and ("daily_projection" in document or _peek(normalized, "daily_projection") is not None):
        check("daily_projection", _daily, _peek(document, "daily_projection"), report, normalized)
        gaps.append("DAILY_RUN_LINKAGE_UNVERIFIED")
    local = "FAIL" if any(row["status"] == "FAIL" for row in findings) else "BLOCKED" if findings else "CONSISTENT"
    return {"schema_version": "qsl.ibkr_source_evidence_result.v1", "status": "FAIL" if local == "FAIL" else "BLOCKED",
            "local_consistency": local, "findings": findings, "checked_groups": checked, "gaps": gaps,
            "execution_authorized": False, "production_ready": False, "historical_parity_established": False,
            "individual_fills_verified": False, "account_domain_authenticated": False, "deployment_verified": False}
