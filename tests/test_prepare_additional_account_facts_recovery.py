import base64
import copy
import json

import pytest

from scripts import prepare_additional_account_facts_recovery as recovery
from scripts import diagnose_account_facts_identity as diagnostic
from tests.test_diagnose_account_facts_identity import _environment, _report, _metadata, NOW, PRIVATE


@pytest.fixture
def case(monkeypatch):
    _, targets = _environment(monkeypatch, target="additional-2")
    expected = diagnostic.resolve_expected()
    report = _report(expected)
    revision = PRIVATE + "-serving-revision"
    report["diagnostics"]["runtime_revision"] = revision
    report["runtime_release_receipt"]["runtime_revision"] = revision
    metadata = _metadata(expected)
    metadata["status"]["traffic"] = [{"revisionName": revision, "percent": 100}]
    return dict(report=report, metadata=metadata, expected=expected,
                raw_targets=json.dumps(targets), slot="additional-2",
                expected_revision_hash=recovery.sha(revision),
                uri=expected["expected_report_prefix"] + "/2026-10/20261013T115958Z.json", now=NOW)


def test_updates_only_selected_revision_and_retains_exact_restore(case):
    original, updated, rotation = recovery.prepare(**case)
    assert original == json.loads(case["raw_targets"])
    assert updated["additional-1"] == original["additional-1"]
    assert updated["additional-3"] == original["additional-3"]
    changed = {k for k in original["additional-2"] if original["additional-2"][k] != updated["additional-2"][k]}
    assert changed == {"runtime_revision"}
    assert rotation["target_id"] == case["expected"]["target_id"]
    assert rotation["previous_source_binding_id"] != rotation["next_source_binding_id"]


def test_failed_report_returns_fixed_diagnostic_fields_without_private_data(case):
    case["report"]["summary"]["account_facts"]["account_ids"] = ["U00000999"]
    with pytest.raises(recovery.RecoveryError) as raised:
        recovery.prepare(**case)
    details = raised.value.details
    assert details["projection_reason"] == "account_identity_mismatch"
    assert details["account_facts_stages"]["account_identity"] != "verified"
    assert PRIVATE not in json.dumps(details) and "U00000999" not in json.dumps(details)


def test_legacy_report_without_optional_receipt_uses_existing_provenance_guards(case):
    case["report"].pop("runtime_release_receipt")
    original, updated, rotation = recovery.prepare(**case)
    assert updated["additional-2"]["runtime_revision"] != original["additional-2"]["runtime_revision"]
    assert rotation["next_source_binding_id"] != rotation["previous_source_binding_id"]


@pytest.mark.parametrize("change", ["slot", "hash", "split", "metadata_service", "report_revision",
                                    "receipt", "account", "scope", "stale", "configured", "duplicate"])
def test_rejects_changed_identity_stale_or_nonserving_evidence(case, change):
    c = copy.deepcopy(case)
    if change == "slot": c["slot"] = "primary-live"
    elif change == "hash": c["expected_revision_hash"] = "f" * 64
    elif change == "split": c["metadata"]["status"]["traffic"] = [{"revisionName": "other", "percent": 50}]
    elif change == "metadata_service": c["metadata"]["metadata"]["name"] = "other"
    elif change == "report_revision": c["report"]["diagnostics"]["runtime_revision"] = "other"
    elif change == "receipt": c["report"]["runtime_release_receipt"]["runtime_revision"] = "other"
    elif change == "account": c["report"]["summary"]["account_facts"]["account_ids"] = ["U00000999"]
    elif change == "scope": c["report"]["account_scope"] = "other"
    elif change == "stale": c["report"]["summary"]["account_facts"]["observed_at"] = "2020-01-01T00:00:00Z"
    elif change == "configured": c["expected"]["expected_runtime_revision"] = "other"
    elif change == "duplicate":
        targets = json.loads(c["raw_targets"])
        targets["additional-1"]["account_selector"] = targets["additional-2"]["account_selector"]
        c["raw_targets"] = json.dumps(targets)
    with pytest.raises(ValueError):
        recovery.prepare(**c)


def test_sealed_artifact_contains_restore_but_no_plaintext(monkeypatch, case):
    values = recovery.prepare(**case)
    encrypted_inputs = []
    def seal(key, value):
        encrypted_inputs.append((key, json.loads(value)))
        return b"opaque-test-ciphertext"
    monkeypatch.setattr(recovery, "_seal_secret", seal)
    key = {"key": base64.b64encode(bytes(range(32))).decode(), "key_id": "test-key"}
    result = recovery.seal_values(*values, {"publisher": key, "receiver": key})
    assert len(result) == 3
    assert [r["purpose"] for r in result] == ["restore", "adopt", "rotate"]
    assert PRIVATE not in json.dumps(result)
    assert [row[1] for row in encrypted_inputs] == list(values)


@pytest.mark.parametrize("keys", [{}, {"publisher": {}}, {"publisher": {}, "receiver": {}},
    {"publisher": {"key": "bad", "key_id": "test"}, "receiver": {"key": "bad", "key_id": "test"}}])
def test_rejects_invalid_protected_recipients_before_sealing(monkeypatch, case, keys):
    monkeypatch.setattr(recovery, "_seal_secret", lambda *_: pytest.fail("invalid key must not seal"))
    with pytest.raises((ValueError, KeyError)):
        recovery.seal_values(*recovery.prepare(**case), keys)
