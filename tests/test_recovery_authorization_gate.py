"""TEST_ONLY: deterministic P01 simulations, never authenticated provider evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from scripts import recovery_authorization_gate as gate


@pytest.fixture
def bundle():
    """All identities are FAKE; no keys, credentials, transport or runtime data."""
    bindings = dict(project="<PROJECT_ID>", repository="FAKE/repository",
                    workflow="FAKE/reader.yml", ref="refs/heads/main",
                    run="FAKE_RUN", attempt=1, run_sha="b" * 40,
                    workflow_sha="b" * 40, operator="b" * 40, reader="b" * 40,
                    source="a" * 40, image="sha256:" + "c" * 64,
                    environment="FAKE_ENVIRONMENT", target="FAKE_TARGET",
                    recipient="FAKE_RECIPIENT", cert_pin="d" * 64,
                    nonce="TEST_ONLY_NONCE", request="FAKE_REQUEST",
                    generation=1, artifact="FAKE_ARTIFACT", mode="receipt")
    c0 = {role: "FAKE_PROOF_" + role for role in gate.C0_ROLES}
    authority = dict(schema="TEST_ONLY_P01_V1", bindings=bindings, c0=c0,
                     issuer="FAKE_ISSUER", claim="FAKE_CLAIM", decision="EXECUTE",
                     issued=100, deadline=200)
    envelope = dict(schema="TEST_ONLY_P01_V1", bindings=deepcopy(bindings),
                    captured=110, expires=190, receipt="TEST_ONLY_READ_VERIFIED",
                    payload_digest=gate.digest(b"TEST_ONLY_PAYLOAD"))
    ciphertext = b"TEST_ONLY_VALID_CIPHERTEXT"
    metadata = dict(schema="TEST_ONLY_P01_V1", bindings=deepcopy(bindings),
                    issuer="FAKE_PRODUCER", conclusion="success",
                    created=111, ciphertext_digest=gate.digest(ciphertext),
                    envelope_digest=gate.digest(gate.encode(envelope)))
    trust = gate.FakeTrust(deepcopy(bindings), deepcopy(c0))
    return dict(authority=authority, envelope=envelope, metadata=metadata,
                ciphertext=ciphertext, payload=b"TEST_ONLY_PAYLOAD", trust=trust)


def validate(b):
    return gate.validate_simulation(**b, now=120)


def denied(b):
    with pytest.raises(gate.Denied, match="^TEST_ONLY validation denied$"):
        validate(b)


def test_consistent_fake_evidence_never_authorizes_execution(bundle):
    assert validate(bundle) == {"status": "SIMULATION_ONLY", "execution_authorized": False,
                                "authentic_provenance": False, "private_delivery": False}


@pytest.mark.parametrize("role", gate.C0_ROLES)
def test_missing_c0_proof_denies(bundle, role):
    del bundle["authority"]["c0"][role]
    denied(bundle)


@pytest.mark.parametrize("value", [True, False, "", "approved", None])
def test_caller_boolean_or_label_is_not_ownership(bundle, value):
    bundle["authority"]["c0"]["recipient"] = value
    denied(bundle)


@pytest.mark.parametrize("field", ["repository", "workflow", "ref", "run", "attempt",
    "run_sha", "workflow_sha", "operator", "reader", "source", "image", "environment",
    "target", "recipient", "cert_pin", "nonce", "request", "generation", "artifact", "mode"])
def test_every_producer_envelope_binding_is_pinned(bundle, field):
    for layer in ("metadata", "envelope", "authority"):
        bad = deepcopy(bundle)
        bad[layer]["bindings"][field] = "FAKE_ATTACKER"
        denied(bad)


def test_valid_attacker_ciphertext_is_not_producer_provenance(bundle):
    bundle["ciphertext"] = b"TEST_ONLY_VALID_ATTACKER_CIPHERTEXT"
    bundle["metadata"]["ciphertext_digest"] = gate.digest(bundle["ciphertext"])
    bundle["metadata"]["issuer"] = "FAKE_ATTACKER"
    denied(bundle)


@pytest.mark.parametrize("layer,field,value", [
    ("metadata", "conclusion", "unknown"), ("metadata", "schema", "unknown"),
    ("metadata", "issuer", True), ("metadata", "ciphertext_digest", "0" * 64),
    ("metadata", "envelope_digest", "0" * 64), ("metadata", "created", 90),
    ("metadata", "created", 121), ("envelope", "captured", 0),
    ("envelope", "captured", 121), ("envelope", "expires", 120),
    ("envelope", "payload_digest", "0" * 64), ("envelope", "receipt", "APPROVED"),
    ("authority", "issuer", "FAKE_ATTACKER"), ("authority", "claim", ""),
    ("authority", "decision", "PLAN_ONLY"), ("authority", "decision", "DISABLED"),
    ("authority", "decision", "SUPERSEDED"), ("authority", "issued", 121),
    ("authority", "deadline", 120), ("authority", "deadline", True),
])
def test_tamper_stale_unknown_or_disabled_denies(bundle, layer, field, value):
    bundle[layer][field] = value
    denied(bundle)


@pytest.mark.parametrize("layer", ["authority", "metadata", "envelope"])
def test_missing_unknown_schema_fields_and_provenance_deny(bundle, layer):
    for value in (None, {}, {**bundle[layer], "approved": True}):
        bad = deepcopy(bundle)
        bad[layer] = value
        denied(bad)


def test_strict_json_rejects_duplicate_unknown_and_nonfinite_values():
    for raw in ('{"a":1,"a":2}', '{"a":NaN}', '[]', '{broken'):
        with pytest.raises(gate.Denied):
            gate.parse_document(raw)
    assert gate.parse_document('{"schema":"TEST_ONLY"}') == {"schema": "TEST_ONLY"}


def test_fixed_diagnostics_never_echo_private_input(bundle, capsys):
    bundle["metadata"]["issuer"] = "FAKE_PRIVATE_DO_NOT_ECHO"
    denied(bundle)
    assert capsys.readouterr() == ("", "")


def new_model(bundle, store=None):
    return gate.FakeGate(bundle, store or gate.FakeStore(), now=lambda: 120)


def test_missing_claim_and_provenance_cannot_create_intent(bundle):
    bundle["authority"]["claim"] = None
    model = new_model(bundle)
    with pytest.raises(gate.Denied):
        model.begin()
    assert model.store.intents == {}


def test_duplicate_and_restart_with_unknown_outcome_never_retry(bundle):
    store = gate.FakeStore()
    first = new_model(bundle, store)
    assert first.begin() == "TEST_ONLY_INTENT"
    with pytest.raises(gate.Denied):
        new_model(bundle, store).begin()
    assert first.finish("unknown") == "TEST_ONLY_UNKNOWN"
    with pytest.raises(gate.Denied):
        new_model(bundle, store).begin()


def test_two_fake_clients_have_one_intent_winner(bundle):
    store = gate.FakeStore()
    def begin(_):
        try:
            return new_model(bundle, store).begin()
        except gate.Denied:
            return "DENIED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(begin, range(2)))
    assert sorted(outcomes) == ["DENIED", "TEST_ONLY_INTENT"]


@pytest.mark.parametrize("change", ["PLAN_ONLY", "DISABLED", "SUPERSEDED", "expiry",
                                    "operator", "main", "claim", "owner"])
def test_latest_state_after_intent_blocks_next_step(bundle, change):
    clock = [120]
    model = gate.FakeGate(bundle, gate.FakeStore(), now=lambda: clock[0])
    model.begin()
    if change == "expiry": clock[0] = 200
    elif change == "operator": bundle["authority"]["bindings"]["operator"] = "e" * 40
    elif change == "main": bundle["trust"].current_main = "e" * 40
    elif change == "claim": bundle["authority"]["claim"] = "FAKE_OTHER_CLAIM"
    elif change == "owner": bundle["authority"]["c0"]["issuer"] = "FAKE_OTHER_OWNER"
    else: bundle["authority"]["decision"] = change
    assert model.finish("success") == "TEST_ONLY_BLOCKED_AFTER_INTENT"
    with pytest.raises(gate.Denied): model.begin()


def test_expiry_before_intent_and_backward_clock_deny(bundle):
    for now in (99, 200, True):
        with pytest.raises(gate.Denied):
            gate.FakeGate(bundle, gate.FakeStore(), now=lambda: now).begin()
    clock = [120]
    model = gate.FakeGate(bundle, gate.FakeStore(), now=lambda: clock[0])
    model.begin()
    clock[0] = 119
    assert model.finish("success") == "TEST_ONLY_BLOCKED_AFTER_INTENT"


def test_lost_intent_and_duplicate_result_deny(bundle):
    model = new_model(bundle)
    model.begin()
    model.store.intents.clear()  # TEST_ONLY simulated integrity loss; never a retry.
    with pytest.raises(gate.Denied): model.finish("success")
    model = new_model(bundle)
    model.begin()
    assert model.finish("success") == "TEST_ONLY_RECORDED"
    with pytest.raises(gate.Denied): model.finish("success")


def test_schema_rejects_real_targets_and_attempt_boolean(bundle):
    for field, value in (("project", "real-target"), ("attempt", True),
                         ("ref", "refs/tags/FAKE"), ("run_sha", "0" * 40)):
        for layer in ("authority", "metadata", "envelope"):
            bundle[layer]["bindings"][field] = value
        bundle["trust"].bindings[field] = value
        denied(bundle)


def test_parser_accepts_fake_roundtrip(bundle):
    raw = json.dumps(bundle["authority"])
    assert gate.parse_document(raw) == bundle["authority"]


def test_replayed_nonce_cannot_gain_new_request_or_generation(bundle):
    store = gate.FakeStore()
    first = new_model(bundle, store)
    first.begin()
    first.finish("success")
    # A new fake card and producer tuple do not make old evidence fresh.
    for layer in ("authority", "envelope", "metadata"):
        bundle[layer]["bindings"]["request"] = "FAKE_NEW_REQUEST"
        bundle[layer]["bindings"]["generation"] = 2
    bundle["trust"].bindings.update(request="FAKE_NEW_REQUEST", generation=2)
    bundle["metadata"]["envelope_digest"] = gate.digest(gate.encode(bundle["envelope"]))
    with pytest.raises(gate.Denied): new_model(bundle, store).begin()


def test_fake_store_clock_integrity_loss_is_fixed_deny(bundle):
    model = new_model(bundle)
    model.begin()
    model.store.last_time.clear()
    assert model.finish("success") == "TEST_ONLY_BLOCKED_AFTER_INTENT"
