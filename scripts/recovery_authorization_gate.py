"""TEST_ONLY P01 local contracts. See AGENTS.md before changing this module.

No provider protocol, cryptography, transport, persistent adapter or runtime entry.
FakeTrust is deliberately unauthenticated. Even a consistent simulation NEVER
establishes authentic authority, producer identity, delivery or execution readiness.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from threading import Lock


C0_ROLES = ("configuration", "admin", "inventory", "recipient", "channel",
            "issuer", "ownership", "policy", "control_store")
_BINDINGS = frozenset(("project", "repository", "workflow", "ref", "run", "attempt",
    "run_sha", "workflow_sha", "operator", "reader", "source", "image", "environment",
    "target", "recipient", "cert_pin", "nonce", "request", "generation", "artifact", "mode"))
_SCHEMA = "TEST_ONLY_P01_V1"


class Denied(ValueError):
    """Fixed diagnostic: never retain or interpolate input values."""

    def __init__(self):
        super().__init__("TEST_ONLY validation denied")


def _require(condition):
    if not condition:
        raise Denied()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def parse_document(raw):
    """Bounded JSON object parser; schema-specific validation remains mandatory."""
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    def invalid(_):
        raise Denied()
    try:
        _require(type(raw) in (str, bytes) and len(raw) <= 65536)
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
        _require(type(value) is dict)
        return value
    except (ValueError, TypeError, RecursionError):
        raise Denied() from None


def _shape(value, fields):
    _require(type(value) is dict and value.keys() == set(fields))


def _integer(value):
    return type(value) is int and value >= 0


def _hex(value, size):
    return (type(value) is str and re.fullmatch(r"[a-f0-9]{%d}" % size, value)
            and value != "0" * size)


class FakeTrust:
    """Harness-supplied FAKE pins, NOT an authenticated authority provider."""

    def __init__(self, bindings, c0):
        self.bindings, self.c0 = deepcopy(bindings), deepcopy(c0)
        self.current_main = bindings.get("run_sha")


def _validate_bindings(b, trust):
    _shape(b, _BINDINGS)
    _require(b == trust.bindings and b["project"] == "<PROJECT_ID>")
    for key in ("repository", "workflow", "run", "environment", "target", "recipient",
                "request", "artifact"):
        _require(type(b[key]) is str and b[key].startswith("FAKE") and len(b[key]) <= 120)
    _require(b["ref"] == "refs/heads/main" and type(b["attempt"]) is int
             and b["attempt"] == 1 and type(b["generation"]) is int and b["generation"] > 0)
    _require(b["mode"] in ("snapshot", "receipt") and type(b["nonce"]) is str
             and b["nonce"].startswith("TEST_ONLY_") and len(b["nonce"]) <= 120)
    for key in ("run_sha", "workflow_sha", "operator", "reader", "source"):
        _require(_hex(b[key], 40))
    _require(b["operator"] == b["run_sha"] == b["workflow_sha"] == b["reader"]
             == trust.current_main)
    _require(type(b["image"]) is str and b["image"].startswith("sha256:")
             and _hex(b["image"][7:], 64) and _hex(b["cert_pin"], 64))


def validate_simulation(*, authority, envelope, metadata, ciphertext, payload, trust, now):
    """Reject inconsistent fake declarations; never return an operational PASS.

    Metadata is a visibly fake stub of authenticated provider observations. No
    field or caller boolean can turn it into genuine provider/owner evidence.
    The fake ciphertext is opaque bytes; CMS validity is not claimed or tested.
    """
    try:
        _require(type(trust) is FakeTrust and _integer(now))
        _shape(authority, ("schema", "bindings", "c0", "issuer", "claim", "decision",
                           "issued", "deadline"))
        _shape(envelope, ("schema", "bindings", "captured", "expires", "receipt",
                          "payload_digest"))
        _shape(metadata, ("schema", "bindings", "issuer", "conclusion", "created",
                          "ciphertext_digest", "envelope_digest"))
        for document in (authority, envelope, metadata):
            _require(document["schema"] == _SCHEMA)
            _validate_bindings(document["bindings"], trust)
        _shape(authority["c0"], C0_ROLES)
        _shape(trust.c0, C0_ROLES)
        for role in C0_ROLES:
            _require(type(authority["c0"][role]) is str
                     and authority["c0"][role] == trust.c0[role] == "FAKE_PROOF_" + role)
        _require(authority["issuer"] == "FAKE_ISSUER" and authority["claim"] == "FAKE_CLAIM"
                 and authority["decision"] == "EXECUTE")
        _require(metadata["issuer"] == "FAKE_PRODUCER" and metadata["conclusion"] == "success")
        for value in (authority["issued"], authority["deadline"], envelope["captured"],
                      envelope["expires"], metadata["created"]):
            _require(_integer(value))
        _require(authority["issued"] <= envelope["captured"] <= metadata["created"] <= now
                 < envelope["expires"] <= authority["deadline"]
                 and now - envelope["captured"] <= 120
                 and 0 < authority["deadline"] - authority["issued"] <= 3600)
        _require(envelope["receipt"] == "TEST_ONLY_READ_VERIFIED")
        for value in (ciphertext, payload):
            _require(type(value) is bytes and value.startswith(b"TEST_ONLY_")
                     and len(value) <= 65536)
        _require(envelope["payload_digest"] == digest(payload)
                 and metadata["ciphertext_digest"] == digest(ciphertext)
                 and metadata["envelope_digest"] == digest(encode(envelope)))
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise Denied() from None
    return dict(status="SIMULATION_ONLY", execution_authorized=False,
                authentic_provenance=False, private_delivery=False)


class FakeStore:
    """In-memory test store shared by fake clients/restarts, no real adapter.

    The lock models an atomic create in one process only. It is not proof of
    cross-process persistence, provider policy, ownership or distributed CAS.
    """

    def __init__(self):
        self.lock = Lock()
        self.intents = {}
        self.results = {}
        self.seen_nonces = set()
        self.last_time = {}


class FakeGate:
    """No actions are performed: only simulated intent/result states are kept."""

    def __init__(self, bundle, store, *, now):
        _require(type(store) is FakeStore)
        self.bundle, self.store, self.now = bundle, store, now
        self.key = None
        self.intent = None

    def _current(self):
        # Revalidate live harness values at EACH boundary, never a cached allow.
        now = self.now()
        validate_simulation(**self.bundle, now=now)
        return now

    def begin(self):
        with self.store.lock:
            now = self._current()
            b = self.bundle["authority"]["bindings"]
            key = (b["generation"], b["request"])
            nonce = (b["recipient"], b["cert_pin"], b["nonce"])
            _require(self.key is None and key not in self.store.intents
                     and nonce not in self.store.seen_nonces)
            intent = encode(self.bundle["authority"])
            self.store.intents[key] = intent
            self.store.seen_nonces.add(nonce)
            self.store.last_time[key] = now
            self.key, self.intent = key, intent
        return "TEST_ONLY_INTENT"

    def finish(self, outcome):
        with self.store.lock:
            _require(self.key is not None and self.key not in self.store.results
                     and self.store.intents.get(self.key) == self.intent)
            _require(type(outcome) is str and outcome in ("success", "failure", "unknown"))
            try:
                now = self._current()
                _require(now >= self.store.last_time[self.key]
                         and encode(self.bundle["authority"]) == self.intent)
            except (Denied, KeyError, TypeError):
                result = "TEST_ONLY_BLOCKED_AFTER_INTENT"
            else:
                result = "TEST_ONLY_UNKNOWN" if outcome == "unknown" else "TEST_ONLY_RECORDED"
            # Expiry/failure/unknown cannot remove intent or recover its allowance.
            self.store.results[self.key] = result
            return result
