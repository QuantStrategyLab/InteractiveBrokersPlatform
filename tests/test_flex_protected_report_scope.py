"""Existing protected slots, not report contents, bound the sole Flex target."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from application import ibkr_flex_source as source
from application.account_facts_target_config import (
    AccountFactsTargetConfigError, account_selector, parse_additional_target_configs, single_line_text,
)
from scripts import diagnose_ibkr_flex_ledger as cli
from scripts import publish_account_facts_from_report as publisher
from tests.test_flex_diagnostic_readonly import configure
from tests.test_ibkr_flex_source import SEND, TOKEN, XML, Session


def protected_config():
    return {f"additional-{index}": {
        "project_id": f"artificial-project-{index}",
        "report_prefix": f"gs://artificial-bucket/reports/{index}",
        "target_id": f"artificial-target-{index}",
        "service_name": f"artificial-service-{index}",
        "runtime_revision": f"artificial-revision-{index}",
        "account_scope": f"artificial-scope-{index}",
        "account_selector": [f"DU000000{index}"],
        "deployment_selector": f"artificial-deployment-{index}",
    } for index in range(1, 4)}


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("scope verification must not invoke external services")
    monkeypatch.setattr(source.requests, "Session", forbidden)
    monkeypatch.setattr(publisher, "build_opener", forbidden)
    monkeypatch.setattr(publisher, "publish_ibkr_account_facts_history", forbidden)


def set_scope(monkeypatch, config=None):
    configure(monkeypatch)
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["DU0000000"]')
    monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON",
        json.dumps(protected_config() if config is None else config))


def test_shared_parser_and_original_facade_have_identical_results_and_error_contract():
    raw = json.dumps(protected_config())
    assert parse_additional_target_configs(raw) == publisher._additional_target_configs(raw)
    for bad in ("{private", '{"additional-1":{},"additional-1":{}}', "[]"):
        with pytest.raises(AccountFactsTargetConfigError) as shared:
            parse_additional_target_configs(bad)
        with pytest.raises(publisher._ProjectionError) as facade:
            publisher._additional_target_configs(bad)
        assert str(shared.value) == str(facade.value) == "target_config_invalid"
        assert facade.value.reason == "target_config_invalid" and facade.value.mismatched_fields == []


@pytest.mark.parametrize("value", [None, "plain", ["A"], ("A", "B"), [" padded"], [1], []])
def test_shared_selector_and_publisher_facade_keep_original_semantics(value):
    assert account_selector(value) == publisher._selector(value)


@pytest.mark.parametrize("value", [None, "plain", " padded", "line\nbreak", "line\rbreak", 1, ""])
def test_shared_single_line_and_publisher_facade_keep_original_semantics(value):
    assert single_line_text(value) == publisher._single_line_text(value)


def test_preflight_validates_complete_scope_without_http(monkeypatch, capsys):
    set_scope(monkeypatch)
    assert cli.main(("--preflight",)) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["status"] == "configuration_shape_valid"
    for private in (TOKEN, "DU0000000", "DU0000001", "artificial-project", "artificial-scope", "gs://"):
        assert private not in out


def test_approved_report_accounts_are_explicitly_forwarded_and_only_primary_is_projected(monkeypatch, capsys):
    set_scope(monkeypatch)
    report = XML.replace(b"</FlexStatements>", b"".join(
        f'<FlexStatement accountId="DU000000{i}" fromDate="20260101" toDate="20260131">'
        '<AccountInformation currency="USD"/><ChangeInNAV fromDate="20260101" '
        'toDate="20260131" twr="999.99"/></FlexStatement>'.encode() for i in range(1, 4))
        + b"</FlexStatements>")
    class ClosedSession(Session):
        def close(self):
            pass
    session = ClosedSession(SEND, report)
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    assert cli.main(("--read-once",)) == 0
    text = capsys.readouterr().out
    out = json.loads(text)
    assert out["currency"] == "EUR"  # The approved additional USD accounts are not projected.
    assert out["period"] == {"from":"2026-01-01", "to":"2026-01-31"}
    assert len(session.calls) == 2
    for private in ("999.99", "DU000000", TOKEN, "artificial-project", "gs://"):
        assert private not in text


@pytest.mark.parametrize("failure", ["primary_absent", "additional_absent", "bad_json", "missing_slot",
    "extra_slot", "extra_field", "missing_field", "duplicate_account", "duplicate_target", "duplicate_prefix",
    "duplicate_json_key", "primary_collision", "wrong_primary", "multiple_primary", "multiple_target"])
def test_partial_invalid_or_mismatched_scope_fails_before_http(monkeypatch, capsys, failure):
    config = protected_config()
    set_scope(monkeypatch, config)
    if failure == "primary_absent": monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON")
    elif failure == "additional_absent": monkeypatch.delenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON")
    elif failure == "bad_json": monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "{private")
    elif failure == "duplicate_json_key":
        raw = json.dumps(config)
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", raw[:-1] + ',"additional-1":{}}')
    elif failure == "wrong_primary": monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["DU0000009"]')
    elif failure == "multiple_primary": monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", '["DU0000000","DU0000009"]')
    elif failure == "multiple_target": monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", '["DU0000000","DU0000001"]')
    else:
        if failure == "missing_slot": del config["additional-3"]
        elif failure == "extra_slot": config["additional-4"] = deepcopy(config["additional-3"])
        elif failure == "extra_field": config["additional-1"]["unexpected"] = "private"
        elif failure == "missing_field": del config["additional-1"]["runtime_revision"]
        elif failure == "duplicate_account": config["additional-2"]["account_selector"] = config["additional-1"]["account_selector"]
        elif failure == "duplicate_target": config["additional-2"]["target_id"] = config["additional-1"]["target_id"]
        elif failure == "duplicate_prefix": config["additional-2"]["report_prefix"] = config["additional-1"]["report_prefix"] + "/"
        elif failure == "primary_collision": config["additional-1"]["account_selector"] = ["DU0000000"]
        monkeypatch.setenv("IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON", json.dumps(config))
    assert cli.main(("--read-once",)) == 2
    text = capsys.readouterr().out
    assert "approved_report_scope_invalid" in json.loads(text)["missing"]
    for private in (TOKEN, "DU000000", "private", "artificial-project", "gs://"):
        assert private not in text


def test_report_cannot_expand_a_valid_approved_scope(monkeypatch, capsys):
    set_scope(monkeypatch)
    report = XML.replace(b"</FlexStatements>", b'<FlexStatement accountId="DU0000009"/></FlexStatements>')
    class ClosedSession(Session):
        def close(self):
            pass
    session = ClosedSession(SEND, report)
    monkeypatch.setattr(source.requests, "Session", lambda: session)
    assert cli.main(("--read-once",)) == 1
    text = capsys.readouterr().out
    assert json.loads(text)["missing"] == ["flex_import_failed"]
    assert "DU0000009" not in text and len(session.calls) == 2


def test_diagnostic_and_pure_config_import_do_not_load_publishers_or_console_url():
    code = """
import sys
from application.account_facts_target_config import parse_additional_target_configs
from scripts import diagnose_ibkr_flex_ledger
assert not any(k.startswith('scripts.publish_') for k in sys.modules)
assert 'scripts.console_config' not in sys.modules
"""
    env = dict(os.environ, QSL_CONSOLE_BASE_URL="invalid-private-origin", PYTHONPATH=str(Path.cwd()))
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
