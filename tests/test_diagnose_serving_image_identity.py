from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import diagnose_serving_image_identity as diagnostic
from scripts import diagnose_account_facts_identity as identity
from scripts import publish_account_facts_from_report as publisher


PRIVATE = "private-never-public"
SOURCE = "a" * 40
UES = "b" * 40
IMAGE = "us-central1-docker.pkg.dev/synthetic-project/runtime/app@sha256:" + "c" * 64
SERVICE = "synthetic-service"
SERVING = SERVICE + "-serving"


@pytest.fixture(autouse=True)
def block_external(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("cloud, Docker or publishing call was not mocked")
    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(publisher, "main", blocked)
    monkeypatch.setattr(publisher, "publish_ibkr_account_facts_history", blocked)
    monkeypatch.setattr(publisher, "build_opener", blocked)


def _env(monkeypatch, target="primary-live"):
    values = {
        "IBKR_ACCOUNT_FACTS_TARGET": target,
        "IBKR_ACCOUNT_FACTS_PROJECT_ID": "synthetic-project",
        "IBKR_ACCOUNT_FACTS_REPORT_PREFIX": "gs://synthetic-private/reports",
        "IBKR_ACCOUNT_FACTS_TARGET_ID": "synthetic-target",
        "IBKR_ACCOUNT_FACTS_SERVICE_NAME": SERVICE,
        "IBKR_ACCOUNT_FACTS_RUNTIME_REVISION": SERVICE + "-old",
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SCOPE": PRIVATE,
        "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON": '["U00000001"]',
        "IBKR_ACCOUNT_FACTS_DEPLOYMENT_SELECTOR": PRIVATE,
        "CLOUD_RUN_REGION": "us-central1",
        "GITHUB_SHA": SOURCE,
    }
    for k, v in values.items(): monkeypatch.setenv(k, v)
    additional = {f"additional-{i}": {
        "project_id": "synthetic-project", "report_prefix": f"gs://synthetic-private/reports-{i}",
        "target_id": f"synthetic-target-{i}", "service_name": SERVICE,
        "runtime_revision": SERVICE + "-old", "account_scope": PRIVATE + f"-{i}",
        "account_selector": [f"U0000001{i}"], "deployment_selector": PRIVATE + f"-{i}",
    } for i in range(1,4)}
    monkeypatch.setenv(publisher.IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON_ENV, json.dumps(additional))


def _service():
    return {"metadata": {"name": SERVICE}, "status": {
        "latestReadyRevisionName": SERVICE + "-candidate",
        "latestCreatedRevisionName": SERVICE + "-candidate",
        "traffic": [{"revisionName": SERVING, "percent": 100},
                    {"revisionName": SERVICE + "-candidate", "percent": 0, "tag": PRIVATE}],
    }, "private": PRIVATE}


def _revision():
    return {"metadata": {"name": SERVING, "labels": {"commit-sha": SOURCE}},
        "spec": {"containers": [{"image": IMAGE, "env": [{"name": "RUNTIME_TARGET_JSON", "value": json.dumps({
            "strategy_release": {"strategy_revision": UES},
            "runtime_risk_limits": {"binding": {"ues_revision": UES}}, "private": PRIVATE,
        })}]}]}, "status": {"imageDigest": IMAGE}, "private": PRIVATE}


def _probe():
    return {"schema_version": 1, "ues_stamp": UES, "packages": {
        "ues": {"present": True, "version": "1.0.0", "commit_id": UES},
        "qpk": {"present": True, "version": "1.0.0", "commit_id": "d" * 40},
        "plugin": {"present": True, "version": "1.0.0", "commit_id": "e" * 40},
    }}


def _fake(monkeypatch, *, service=None, revision=None, probe=None, failure=None, changed=False, readback=None):
    commands=[]; service_reads=0
    def run(command, **kwargs):
        nonlocal service_reads
        commands.append(command)
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        if failure and failure in " ".join(command):
            return subprocess.CompletedProcess(command, 1, PRIVATE, PRIVATE)
        payload = None
        if command[:4] == ("gcloud", "run", "services", "describe"):
            service_reads += 1
            payload = _service() if service is None else service
            if changed and service_reads > 1:
                payload = _service(); payload["status"]["traffic"] = [{"revisionName": SERVICE + "-changed", "percent": 100}]
        elif command[:4] == ("gcloud", "run", "revisions", "describe"):
            assert command[4] == SERVING
            payload = _revision() if revision is None else revision
        elif command[:3] == ("gcloud", "auth", "configure-docker"):
            assert command[3] == "us-central1-docker.pkg.dev"
            assert kwargs["env"]["DOCKER_CONFIG"]
        elif command[:2] == ("docker", "pull"):
            assert command[2] == IMAGE
        elif command[:3] == ("docker", "image", "inspect"):
            assert command[3] == IMAGE
            payload = ([{"RepoDigests": [IMAGE], "Config": {"Labels": {"org.opencontainers.image.revision": SOURCE}}}]
                       if readback is None else readback)
        else:
            assert command[:2] == ("docker", "run")
            assert IMAGE in command
            assert "--network=none" in command and "--read-only" in command
            assert "--cap-drop=ALL" in command and "--security-opt=no-new-privileges" in command
            assert "--user=1000:1000" in command
            assert "-I" in command and "-S" in command
            assert "--entrypoint=/usr/local/bin/python" in command
            assert not any(x in command for x in ("-e", "--env", "--mount", "-v"))
            payload = _probe() if probe is None else probe
        return subprocess.CompletedProcess(command, 0, "" if payload is None else json.dumps(payload), PRIVATE)
    monkeypatch.setattr(subprocess, "run", run)
    return commands


@pytest.mark.parametrize("target", ["primary-live", "additional-1", "additional-2", "additional-3"])
def test_full_readonly_chain_uses_actual_serving_immutable_image_and_sandbox(monkeypatch, capsys, target):
    _env(monkeypatch, target)
    commands = _fake(monkeypatch)
    assert diagnostic.main([]) == 0
    captured = capsys.readouterr()
    result=json.loads(captured.out)
    assert result["status"] == "observed"
    assert result["serving_traffic_status"] == "single"
    assert result["immutable_image_status"] == "verified"
    assert result["equalities"]["configured_equals_serving"] is False
    assert result["equalities"]["revision_source_equals_image_source"] is True
    assert result["equalities"]["ues_stamp_equals_installed_commit"] is True
    assert result["equalities"]["ues_equals_deployed_release"] is True
    assert result["equalities"]["ues_equals_deployed_risk_binding"] is True
    assert all(row["status"] == "observed" for row in result["dependencies"].values())
    assert result["independent_release_approval"] == "unknown"
    assert captured.err == ""
    for private in [PRIVATE, IMAGE, SERVING, UES, "d"*40, "e"*40, "1.0.0"]:
        assert private not in captured.out
    assert len(commands) == 7


@pytest.mark.parametrize("target", ["", "disabled", "additional-4", "ingress-diagnostic"])
def test_unknown_slot_fails_before_authentication_or_reads(monkeypatch, capsys, target):
    _env(monkeypatch,target)
    assert diagnostic.main(["--preflight"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "target_disabled"


def test_bad_config_and_preflight_are_network_free(monkeypatch, capsys):
    _env(monkeypatch)
    assert diagnostic.main(["--preflight"]) == 0
    result=json.loads(capsys.readouterr().out)
    assert result["status"] == "preflight"
    assert all(v is None for v in result["equalities"].values())
    monkeypatch.delenv("IBKR_ACCOUNT_FACTS_PROJECT_ID")
    assert diagnostic.main(["--preflight"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "target_config_unavailable"


@pytest.mark.parametrize("traffic", [None, [], [{"revisionName": SERVING, "percent": 99}],
    [{"revisionName": SERVING, "percent": 50}, {"revisionName": SERVICE+"-other", "percent": 50}],
    [{"percent":100}], [{"revisionName":SERVING,"percent":True}]])
def test_unresolved_traffic_does_not_read_ready_candidate_or_pull(monkeypatch, capsys, traffic):
    _env(monkeypatch)
    service=_service();service["status"]["traffic"]=traffic
    commands=_fake(monkeypatch,service=service)
    assert diagnostic.main([]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "serving_traffic_unresolved"
    assert len(commands)==1


@pytest.mark.parametrize("mutation", ["wrong_revision", "two_containers", "missing_container", "tag_only", "digest_mismatch", "foreign_registry", "wrong_repository"])
def test_invalid_revision_or_image_prevents_pull(monkeypatch,capsys,mutation):
    _env(monkeypatch); revision=_revision()
    if mutation=="wrong_revision": revision["metadata"]["name"]=PRIVATE
    elif mutation=="two_containers": revision["spec"]["containers"]*=2
    elif mutation=="missing_container": revision["spec"]["containers"]=[]
    elif mutation=="tag_only":
        revision["spec"]["containers"][0]["image"]=IMAGE.split("@")[0]+":latest";revision["status"]={}
    elif mutation=="digest_mismatch": revision["status"]["imageDigest"]=IMAGE[:-64]+"f"*64
    elif mutation=="foreign_registry":
        revision["spec"]["containers"][0]["image"]="evil.invalid/repo@sha256:"+"c"*64
        revision["status"]["imageDigest"]=revision["spec"]["containers"][0]["image"]
    else: revision["status"]["imageDigest"]=IMAGE.replace("/runtime/app", "/other/app")
    commands=_fake(monkeypatch,revision=revision)
    assert diagnostic.main([])==1
    result=json.loads(capsys.readouterr().out)
    assert result["status"]=="unknown"
    assert not any(c[0]=="docker" for c in commands)
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("failure", ["services describe", "revisions describe", "configure-docker", "docker pull", "image inspect", "docker run"])
def test_external_failures_are_fixed_redacted_unknown(monkeypatch,capsys,failure):
    _env(monkeypatch);_fake(monkeypatch,failure=failure)
    assert diagnostic.main([])==1
    captured=capsys.readouterr();result=json.loads(captured.out)
    assert result["status"]=="unknown"
    assert PRIVATE not in captured.out+captured.err
    assert result["independent_release_approval"]=="unknown"


def test_serving_drift_invalidates_current_image_association(monkeypatch,capsys):
    _env(monkeypatch);_fake(monkeypatch,changed=True)
    assert diagnostic.main([])==1
    result=json.loads(capsys.readouterr().out)
    assert result["reason"]=="serving_changed_during_probe"
    assert result["status"]=="unknown"


def test_missing_release_binding_is_unknown_and_not_compared_to_main_or_pin(monkeypatch,capsys):
    _env(monkeypatch);revision=_revision();revision["spec"]["containers"][0]["env"]=[]
    _fake(monkeypatch,revision=revision)
    assert diagnostic.main([])==0
    result=json.loads(capsys.readouterr().out)
    assert result["equalities"]["ues_equals_deployed_release"] is None
    assert result["equalities"]["ues_equals_deployed_risk_binding"] is None
    assert result["independent_release_approval"]=="unknown"


@pytest.mark.parametrize("mutation", ["missing_plugin", "wheel_qpk", "bad_commit", "bad_version", "private_nested", "bad_schema"])
def test_partial_or_malformed_install_metadata_never_fabricates_commit_equality(monkeypatch,capsys,mutation):
    _env(monkeypatch);probe=_probe()
    if mutation=="missing_plugin": probe["packages"]["plugin"]={"present":False,"version":None,"commit_id":None}
    elif mutation=="wheel_qpk": probe["packages"]["qpk"]["commit_id"]=None
    elif mutation=="bad_commit": probe["packages"]["ues"]["commit_id"]=PRIVATE
    elif mutation=="bad_version": probe["packages"]["ues"]["version"]={"private":PRIVATE}
    elif mutation=="private_nested": probe["packages"]["ues"]["commit_id"]={"nested":PRIVATE}
    else: probe["schema_version"]=PRIVATE
    _fake(monkeypatch,probe=probe)
    assert diagnostic.main([])==1
    captured=capsys.readouterr();result=json.loads(captured.out)
    assert result["status"] in {"partial","unknown"}
    assert PRIVATE not in captured.out+captured.err
    if mutation in {"bad_commit","private_nested","bad_schema"}:
        assert result["equalities"]["ues_equals_deployed_release"] is None


def test_probe_code_is_stdlib_metadata_only_and_does_not_import_business_packages():
    code=diagnostic.IMAGE_PROBE
    assert "metadata.distributions" in code
    assert "/app/.venv/lib/python" in code
    assert not any(x in code for x in ["import main", "import us_equity", "import quant_platform", "import quant_strategy", "socket", "requests", "subprocess"])


def test_workflow_manual_main_preflight_before_auth_without_business_channels():
    text=Path('.github/workflows/serving-image-identity-diagnose.yml').read_text()
    assert "workflow_dispatch:" in text and "github.ref == 'refs/heads/main'" in text
    assert "schedule:" not in text
    assert text.index('-m scripts.diagnose_serving_image_identity --preflight') < text.index('google-github-actions/auth')
    assert "SYNC_TOKEN" not in text and "TELEGRAM" not in text and "upload-artifact" not in text
    assert all(t in text for t in ['primary-live','additional-1','additional-2','additional-3'])


def test_inert_import_and_invalid_argv_do_not_run_cloud_or_docker(monkeypatch,capsys):
    _env(monkeypatch)
    assert diagnostic.main([PRIVATE])==1
    assert PRIVATE not in capsys.readouterr().out
    assert callable(identity.resolve_expected)


@pytest.mark.parametrize("resolved", [IMAGE, "sha256:" + "c" * 64])
def test_mutable_spec_is_only_usable_with_exact_revision_resolved_digest(monkeypatch,capsys,resolved):
    _env(monkeypatch);revision=_revision()
    revision["spec"]["containers"][0]["image"]=IMAGE.split('@')[0]+':latest'
    revision["status"]["imageDigest"]=resolved
    _fake(monkeypatch,revision=revision)
    assert diagnostic.main([])==0
    assert json.loads(capsys.readouterr().out)["immutable_image_status"]=="verified"


def test_mutable_resolved_value_is_not_digest_proof_even_with_pinned_spec(monkeypatch,capsys):
    _env(monkeypatch);revision=_revision()
    revision["status"]["imageDigest"]=IMAGE.split('@')[0]+':latest'
    commands=_fake(monkeypatch,revision=revision)
    assert diagnostic.main([])==1
    assert not any(c[0]=='docker' for c in commands)
    assert json.loads(capsys.readouterr().out)["reason"]=="immutable_image_unavailable"


@pytest.mark.parametrize("readback", [[], [{"RepoDigests":[]}], [{"RepoDigests":[PRIVATE]}],
    [{"RepoDigests":[IMAGE]},{"RepoDigests":[IMAGE]}]])
def test_missing_or_ambiguous_docker_digest_readback_prevents_probe(monkeypatch,capsys,readback):
    _env(monkeypatch);commands=_fake(monkeypatch,readback=readback)
    assert diagnostic.main([])==1
    result=json.loads(capsys.readouterr().out)
    assert result['reason']=='image_readback_mismatch'
    assert not any(c[:2]==('docker','run') for c in commands)
    assert PRIVATE not in json.dumps(result)


def test_missing_image_source_label_does_not_infer_source_from_current_workflow(monkeypatch,capsys):
    _env(monkeypatch);_fake(monkeypatch,readback=[{"RepoDigests":[IMAGE],"Config":{"Labels":{}}}])
    assert diagnostic.main([])==0
    result=json.loads(capsys.readouterr().out)
    assert result['equalities']['revision_source_equals_image_source'] is None
    assert result['independent_release_approval']=='unknown'


def test_missing_direct_url_commit_is_not_replaced_with_ues_stamp(monkeypatch,capsys):
    _env(monkeypatch);probe=_probe();probe['packages']['ues']['commit_id']=None
    _fake(monkeypatch,probe=probe)
    assert diagnostic.main([])==1
    result=json.loads(capsys.readouterr().out)
    assert result['dependencies']['ues']['status']=='partial'
    assert result['equalities']['ues_stamp_equals_installed_commit'] is None
    assert result['equalities']['ues_equals_deployed_release'] is None


def test_fresh_interpreter_import_is_inert(monkeypatch):
    code='''
import socket,subprocess,urllib.request
def blocked(*args,**kwargs):
    raise AssertionError("external call while importing")
socket.socket=blocked
socket.getaddrinfo=blocked
subprocess.run=blocked
urllib.request.urlopen=blocked
urllib.request.build_opener=blocked
import scripts.diagnose_serving_image_identity
print("inert import passed")
'''
    monkeypatch.undo()
    result=subprocess.run([sys.executable,'-I','-c',"import sys; sys.path.insert(0,'.');\n"+code],capture_output=True,text=True,check=False)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='inert import passed'


def test_isolated_metadata_probe_reads_fixture_files_without_startup_or_business_imports(monkeypatch,tmp_path):
    packages=tmp_path/'venv/lib/python3.12/site-packages';packages.mkdir(parents=True)
    for alias,name in diagnostic.PACKAGES.items():
        dist=packages/(name.replace('-','_')+'-1.0.0.dist-info');dist.mkdir()
        (dist/'METADATA').write_text('Metadata-Version: 2.1\nName: '+name+'\nVersion: 1.0.0\n')
        (dist/'direct_url.json').write_text(json.dumps({'vcs_info':{'commit_id':UES}}))
        (packages/(name.replace('-','_')+'.py')).write_text('raise AssertionError("business module imported")\n')
    (packages/'sitecustomize.py').write_text('raise AssertionError("startup hook imported")\n')
    stamp=tmp_path/'UES_REVISION';stamp.write_text(UES+'\n')
    probe=diagnostic.IMAGE_PROBE.replace('/app/.venv/lib/python',str(tmp_path/'venv/lib/python'))
    probe=probe.replace('/usr/local/lib/python',str(tmp_path/'empty/lib/python')).replace('/app/UES_REVISION',str(stamp))
    monkeypatch.undo()
    result=subprocess.run([sys.executable,'-I','-S','-c',probe],capture_output=True,text=True,check=False,
        env={'PYTHONPATH':str(packages)})
    assert result.returncode==0,result.stderr
    assert result.stderr==''
    payload=json.loads(result.stdout)
    statuses,commits,read_stamp=diagnostic.project_install_metadata(payload)
    assert all(row['status']=='observed' for row in statuses.values())
    assert all(commit==UES for commit in commits.values())
    assert read_stamp==UES
