"""Synthetic bundles/configuration and subprocess-local guards; no cloud IO."""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from runtime_config_support import AccountGroupConfig
from scripts import deploy_pilot_no_traffic as pilot
from scripts import pilot_app_isolation as isolation
from scripts import pilot_app_isolation_entry as entry
from scripts import run_pilot_job_tcp as job
from tests.test_pilot_no_traffic_release import baseline, environment


def archive(rows):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, data in rows:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_runtime_bundle_contains_frozen_runtime_files_and_retains_lock(monkeypatch, tmp_path):
    lock = b"synthetic locked dependencies"
    raw = archive([("main.py", b"print('synthetic')"), ("uv.lock", lock),
        ("application/config.py", b"pass"), ("config/runtime.json", b"{}"),
        ("tests/fixture.py", b"pass"), ("docs/notes.md", b"example")])
    data, digest = isolation.make_bundle(raw)
    monkeypatch.setattr(entry, "LOCK", hashlib.sha256(lock).hexdigest())
    source = tmp_path / "source"
    source.mkdir()
    entry.unpack(data, digest, source)
    assert (source / "main.py").exists() and (source / "application/config.py").exists()
    assert (source / "config/runtime.json").exists() and (source / "uv.lock").read_bytes() == lock
    assert not (source / "tests").exists() and not (source / "docs").exists()


@pytest.mark.parametrize("names", [["../outside"], ["/tmp/outside"], ["main.py", "main.py"]])
def test_bundle_rejects_path_escape_and_duplicates(tmp_path, names):
    data = gzip.compress(archive([(name, b"synthetic") for name in names]), mtime=0)
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(entry.IsolationError, match="candidate_bundle_unverified"):
        entry.unpack(data, hashlib.sha256(data).hexdigest(), source)
    assert not (tmp_path / "outside").exists()


def test_bundle_digest_mismatch_rejects_before_extraction(tmp_path):
    with pytest.raises(entry.IsolationError, match="candidate_bundle_unverified"):
        entry.unpack(b"synthetic", "f" * 64, tmp_path)
    assert not list(tmp_path.iterdir())


def test_source_must_be_exact_candidate_before_archiving(monkeypatch, tmp_path):
    calls = []
    def git(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"0" * 40 + b"\n", b"")
    monkeypatch.setattr(isolation.subprocess, "run", git)
    with pytest.raises(pilot.ReleaseError, match="candidate_source_unverified"):
        isolation.bundle(tmp_path)
    assert len(calls) == 1


def test_application_job_retains_binding_but_drops_notification_and_credential_routes(monkeypatch):
    ctx = pilot.context(environment())
    before = baseline()
    revision = before["serving_revision"]
    revision["spec"].update(serviceAccountName="synthetic-sa")
    revision["metadata"]["annotations"]["run.googleapis.com/network-interfaces"] = '[{"network":"synthetic"}]'
    revision["spec"]["containers"][0].update(
        image="us-central1-docker.pkg.dev/synthetic-project/runtime/app@" + job.cloud.BASE,
        resources={"limits": {"cpu": "1", "memory": "512Mi"}})
    env = revision["spec"]["containers"][0]["env"]
    env.extend([dict(name="ACCOUNT_GROUP", value="synthetic"), dict(name="GLOBAL_TELEGRAM_CHAT_ID", value="synthetic-chat"),
        dict(name="NOTIFICATION_WEBHOOK_URL", value="https://synthetic.invalid"),
        dict(name="IB_ACCOUNT_GROUP_CONFIG_SECRET_NAME", value="synthetic-secret"),
        dict(name="IBKR_API_KEY", valueFrom={"secretKeyRef": {"name": "synthetic-key", "key": "latest"}})])
    group = AccountGroupConfig(execution_backend="gateway", ib_gateway_instance_name="192.0.2.1",
        ib_gateway_mode="live", ib_gateway_port=4001, ib_client_id=7, account_ids=tuple(ctx.target["account_selector"]))
    tcp = job.prepared(ctx, before, group, "123")
    monkeypatch.setattr(isolation, "bundle", lambda _: (b"public synthetic bundle", "a" * 64))
    original = copy.deepcopy(before)
    desired = isolation.prepared(ctx, before, group, tcp, Path("source"))
    task = desired["spec"]["template"]["spec"]["template"]["spec"]
    container = task["containers"][0]
    assert task["timeoutSeconds"] == "120" and task["maxRetries"] == 0
    assert container["command"] == ["/app/.venv/bin/python"]
    values = {row["name"]: row.get("value") for row in container["env"]}
    assert not any(isolation.SENSITIVE.search(name) for name in values)
    assert values["ACCOUNT_GROUP"] == "synthetic"
    assert json.loads(values["IB_ACCOUNT_GROUP_CONFIG_JSON"])["groups"]["synthetic"]["ib_client_id"] == 7
    assert json.loads(values["PILOT_EXPECTED_TARGET"])["target"]["account_selector"] == ctx.target["account_selector"]
    assert before == original and tcp["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] == "60"


def test_audit_rejects_network_subprocess_credentials_and_outside_writes(tmp_path):
    definitions = Path(entry.__file__).read_text().split('if __name__ == "__main__":')[0]
    script = definitions + '\n' + '''
root = Path(sys.argv[1]).resolve()
violations = []
install_audit(root, violations)
blocked = []
for effect in (lambda: (root.parent / "outside-forbidden").write_text("blocked"),
        lambda: __import__("socket").getaddrinfo("synthetic.invalid", 1),
        lambda: __import__("subprocess").run(["false"]),
        lambda: Path("/synthetic/.config/gcloud/application_default_credentials.json").read_text()):
    try:
        effect()
        blocked.append(False)
    except IsolationError:
        blocked.append(True)
(root / "canary").write_text("allowed")
print(json.dumps(dict(blocked=blocked, violations=violations, readback=(root / "canary").read_text())))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == dict(blocked=[True] * 4, violations=["outside_path", "network", "subprocess", "credential_path"], readback="allowed")
    assert not (tmp_path.parent / "outside-forbidden").exists()


def test_application_measurement_rejects_unreviewed_output_fields():
    with pytest.raises(pilot.ReleaseError, match="application_response_invalid"):
        isolation.measurement([{"jsonPayload": {"unexpected": "private-untrusted-text"}}])
