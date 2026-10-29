"""Exercise the existing workflow's scheduler step with synthetic cloud metadata."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("desired,initial,action", [("true", "ENABLED", None), ("true", "PAUSED", "resume"), ("false", "ENABLED", "pause")])
def test_switch_scheduler_only_changes_existing_job(tmp_path, desired, initial, action):
    workflow = Path(".github/workflows/sync-cloud-run-env-protected.yml").read_text()
    step = workflow.split("- name: Reconcile existing runtime switch scheduler", 1)[1].split("- name: Sync Cloud Scheduler schedule", 1)[0]
    source = step.split("uv run --no-sync python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    source = textwrap.dedent(source)
    path = tmp_path / "cloud.json"
    actions = tmp_path / "actions.json"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "candidate-revision.txt").write_text("unit-service-rev")
    if action is None:
        (evidence / "no-op").touch()
    revision = {"metadata": {"name": "unit-service-rev", "labels": {"commit-sha": "synthetic"}},
                "spec": {"containers": [{"image": "image@sha256:synthetic", "startupProbe": {"httpGet": {"path": "/health", "port": 8080}},
                                         "env": [{"name": "RUNTIME_TARGET_JSON", "value": "{}"}, {"name": "RUNTIME_TARGET_ENABLED", "value": desired}]}]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "ContainerHealthy", "status": "True"}]}}
    service = {"metadata": {"resourceVersion": "1"}, "status": {"url": "https://service.invalid", "traffic": [{"percent": 100, "revisionName": "unit-service-rev"}]}}
    job = {"name": "projects/p/locations/r/jobs/test", "state": initial, "httpTarget": {"uri": "https://service.invalid/run"}, "schedule": "0 1 * * 1-5", "timeZone": "UTC"}
    path.write_text(json.dumps({"service": service, "revision": revision, "job": job}))
    actions.write_text("[]")
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json, os, sys
        from pathlib import Path
        path, log = Path(os.environ['FAKE_CLOUD']), Path(os.environ['FAKE_ACTIONS'])
        data = json.loads(path.read_text())
        args = sys.argv[1:]
        if args[:3] == ['run', 'services', 'describe']: out = data['service']
        elif args[:3] == ['run', 'revisions', 'describe']: out = data['revision']
        elif args[:3] == ['scheduler', 'jobs', 'list']: out = [data['job']]
        elif args[:3] == ['scheduler', 'jobs', 'describe']: out = data['job']
        elif args[:3] in (['scheduler', 'jobs', 'resume'], ['scheduler', 'jobs', 'pause']):
            rows = json.loads(log.read_text()); rows.append(args[2]); log.write_text(json.dumps(rows))
            data['job']['state'] = 'ENABLED' if args[2] == 'resume' else 'PAUSED'; path.write_text(json.dumps(data)); out = {}
        else: sys.exit(1)
        print(json.dumps(out))
    '''))
    gcloud.chmod(0o755)
    plan = {"targets": [{"service_name": "unit-service", "env": {"RUNTIME_TARGET_JSON": "{}", "RUNTIME_TARGET_ENABLED": desired}, "standard_execution_enabled": desired == "true"}]}
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE_CLOUD": str(path), "FAKE_ACTIONS": str(actions),
           "IBKR_ENV_SYNC_EVIDENCE_DIR": str(evidence), "SYNC_PLAN_JSON": json.dumps(plan), "GCP_PROJECT_ID": "synthetic",
           "CLOUD_RUN_REGION": "r", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
           "INPUT_REQUEST_ID": "12345678-1234-1234-1234-123456789abc", "RUNNER_TEMP": str(tmp_path)}
    if desired == "false":
        baseline_path = tmp_path / "baseline-service.json"
        baseline_path.write_text(json.dumps(service))
        result_path = tmp_path / "ibkr-runtime-switch-result.json"
        result_path.write_text(json.dumps({"status": "unknown", "platform_applied": False}))
        stop_step = workflow.split('if [ "${desired_switch}" = "false" ]; then', 1)[1].split('if [ "${switch_unchanged}"', 1)[0]
        stop_source = textwrap.dedent(stop_step.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0])
        subprocess.run([sys.executable, "-c", stop_source, str(baseline_path)], env=env, text=True, capture_output=True, check=True)
        partial = json.loads(result_path.read_text())
        assert partial["status"] == "partial" and partial["scheduler_state"] == "PAUSED"
    result = subprocess.run([sys.executable, "-c", source], env=env, text=True, capture_output=True, check=True)
    output = json.loads(result.stdout)
    assert output["enabled"] is (desired == "true") and output["scheduler_state"] == ("ENABLED" if desired == "true" else "PAUSED")
    assert output["run_id"] == "123" and output["attempt"] == "1"
    assert output["request_id"] == env["INPUT_REQUEST_ID"]
    assert output["service_changed"] is (action is not None)
    assert output["scheduler_changed"] is (action is not None)
    assert json.loads((tmp_path / "ibkr-runtime-switch-result.json").read_text()) == output
    assert json.loads(actions.read_text()) == ([action] if action else [])
    assert "unit-service" not in result.stdout
