"""Offline synthetic evidence; never contact IBKR or publish records."""
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from application import ibkr_flex_source as source
from application.ibkr_flex_executions import diagnose_flex_execution_source
from scripts import diagnose_ibkr_flex_ledger as cli


PERIOD = {"from": "2026-10-01", "to": "2026-10-27"}
ACCOUNT = "DU0000000"
TOKEN = "synthetic-private-token"
REFERENCE = "987654321098765"
SEND = f"<FlexStatementResponse><Status>Success</Status><ReferenceCode>{REFERENCE}</ReferenceCode></FlexStatementResponse>".encode()
PENDING = b"<FlexStatementResponse><ErrorCode>1019</ErrorCode></FlexStatementResponse>"


def trade(**changes):
    attrs = dict(accountId=ACCOUNT, levelOfDetail="EXECUTION", ibExecID="synthetic.execution.01",
                 tradeID="private-trade-id", tradeDate="20261023", dateTime="20261023;15:46:44",
                 buySell="SELL", quantity="-1", tradePrice="66.325", currency="USD", assetCategory="STK",
                 symbol="PRIVATE", ibCommission="-0.351826545", taxes="0", ibCommissionCurrency="USD",
                 notes="", origTradeID="0", origExecID="0", origTradeDate="0000-00-00", origTradePrice="0", origOrderID="0")
    attrs.update(changes)
    return ET.Element("Trade", {key: value for key, value in attrs.items() if value is not None})


def statement(*rows):
    stmt = ET.Element("FlexStatement", accountId=ACCOUNT, fromDate="20261001", toDate="20261027")
    ET.SubElement(stmt, "AccountInformation", currency="USD")
    ET.SubElement(stmt, "Trades").extend(rows)
    return stmt


def report(*rows):
    root = ET.Element("FlexQueryResponse", type="AF")
    ET.SubElement(root, "FlexStatements").append(statement(*rows))
    return ET.tostring(root)


def diagnostic(*rows):
    return diagnose_flex_execution_source(statement(*rows), source_ref="a" * 64, period=PERIOD)


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, **_):
        yield self.body


class Session:
    def __init__(self, *bodies):
        self.bodies = iter(bodies)
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.calls.append((url.rsplit("/", 1)[-1], kwargs["params"]))
        return Response(next(self.bodies))


@pytest.fixture(autouse=True)
def no_real_session(monkeypatch):
    monkeypatch.setattr(source.requests, "Session", lambda: pytest.fail("unexpected network session"))


def test_distinct_ids_and_fee_difference_remain_evidence_not_deduplication():
    result = diagnostic(trade(), trade(ibExecID="synthetic.execution.02", ibCommission="-0.001569295"))
    assert result["counts"]["suffix_family_candidate_groups"] == 1
    assert result["counts"]["explicit_correction_signal_rows"] == 0
    assert result["counts"]["repeated_identity_groups"] == 0
    group = result["economic_similarity_groups"][0]
    assert group["distinct_native_id_count"] == 2 and group["fee_comparison"] == "different"
    assert group["suffix_family_candidate_groups"] == 1
    assert group["explicit_correction_signal_rows"] == 0
    assert result["correction_resolution"] == "not_applied"
    assert result["publication_authority_granted"] is False
    assert result["coverage"] == "not_evaluated"
    assert cli._validated_execution_report(result) == result
    output = json.dumps(result)
    for private in (ACCOUNT, "synthetic.execution", "private-trade-id", "PRIVATE", "66.325", "0.351826545", "0.001569295"):
        assert private not in output


def test_same_economics_different_families_are_not_corrections():
    result = diagnostic(trade(), trade(ibExecID="unrelated.other.01"))
    assert result["counts"]["suffix_family_candidate_groups"] == 0
    assert result["economic_similarity_groups"][0]["distinct_native_id_count"] == 2
    assert result["economic_similarity_groups"][0]["fee_comparison"] == "same"


def test_exact_repeat_conflict_and_explicit_signals_are_independent():
    result = diagnostic(trade(), trade(), trade(ibCommission="-0.1", notes="CO;PRIVATE-NOTE", origExecID="private-origin-id"))
    assert result["counts"]["repeated_identity_groups"] == 1
    assert result["counts"]["identity_conflict_groups"] == 1
    assert result["counts"]["explicit_correction_signal_rows"] == 1
    assert result["counts"]["notes_correction_signal_rows"] == 1
    assert result["counts"]["original_reference_signal_rows"] == 1
    assert "PRIVATE-NOTE" not in json.dumps(result)
    assert "private-origin-id" not in json.dumps(result)


def test_missing_native_fields_stay_unknown():
    result = diagnostic(trade(notes=None, origExecID=None, ibCommission=None), trade(ibExecID=None))
    assert result["counts"]["notes_unknown_rows"] == 1
    assert result["counts"]["original_fields_unknown_rows"] == 1
    assert result["counts"]["missing_identity_rows"] == 1
    assert result["field_counts"]["origExecID"]["present"] == 1
    assert result["economic_similarity_groups"][0]["fee_comparison"] == "unknown"


def test_comparison_domains_are_random_per_report(monkeypatch):
    from application import ibkr_flex_executions as executions
    monkeypatch.setattr(executions.secrets, "token_bytes", lambda size: b"a" * size)
    first = diagnostic(trade(), trade())
    assert first == diagnostic(trade(), trade())
    monkeypatch.setattr(executions.secrets, "token_bytes", lambda size: b"b" * size)
    second = diagnostic(trade(), trade())
    assert first["economic_similarity_groups"][0]["comparison_sha256"] != second["economic_similarity_groups"][0]["comparison_sha256"]
    assert "nonce" not in json.dumps(second)


def test_source_requests_exact_window_and_collects_same_handle(monkeypatch):
    delays = []
    monkeypatch.setattr(source.time, "sleep", delays.append)
    session = Session(SEND, PENDING, report(trade()))
    result = source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345", expected_account_ids=(ACCOUNT,),
        session=session, collect_pending=True, execution_source_only=True, report_period=PERIOD)
    assert result["period"] == PERIOD
    assert session.calls[0] == ("SendRequest", {"t": TOKEN, "q": "12345", "v": "3", "fd": "20261001", "td": "20261027"})
    assert session.calls[1:] == [("GetStatement", {"t": TOKEN, "q": REFERENCE, "v": "3"})] * 2
    assert delays == [1, 3]


def test_pending_is_bounded_and_never_generates_replacement(monkeypatch):
    delays = []
    monkeypatch.setattr(source.time, "sleep", delays.append)
    session = Session(SEND, *([PENDING] * 6))
    with pytest.raises(source.FlexReportPending):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345", expected_account_ids=(ACCOUNT,),
            session=session, collect_pending=True, execution_source_only=True, report_period=PERIOD)
    assert len(session.calls) == 7
    assert [name for name, _ in session.calls].count("SendRequest") == 1
    assert delays == [1, 3, 3, 3, 3, 3]


@pytest.mark.parametrize("code", ["1012", "1013", "1014", "1015", "1017", "1018", "1021"])
def test_permanent_provider_failures_stop_without_retry(monkeypatch, code):
    monkeypatch.setattr(source.time, "sleep", lambda _: None)
    session = Session(SEND, f"<FlexStatementResponse><ErrorCode>{code}</ErrorCode></FlexStatementResponse>".encode())
    with pytest.raises(source.FlexSourceError):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345", expected_account_ids=(ACCOUNT,),
            session=session, collect_pending=True, execution_source_only=True, report_period=PERIOD)
    assert len(session.calls) == 2


@pytest.mark.parametrize("mutation", ["account", "unknown_account", "period", "date", "type"])
def test_scope_period_and_native_date_checks_remain_fail_closed(monkeypatch, mutation):
    monkeypatch.setattr(source.time, "sleep", lambda _: None)
    root = ET.fromstring(report(trade()))
    stmt = root.find("./FlexStatements/FlexStatement")
    if mutation == "account":
        stmt.find("./Trades/Trade").set("accountId", "DU9999999")
    elif mutation == "unknown_account":
        ET.SubElement(root.find("FlexStatements"), "FlexStatement", accountId="DU9999999")
    elif mutation == "period":
        stmt.set("toDate", "20261026")
    elif mutation == "date":
        stmt.find("./Trades/Trade").set("tradeDate", "20260930")
    else:
        root.set("type", "CONFIRM")
    session = Session(SEND, ET.tostring(root))
    with pytest.raises(ValueError):
        source.diagnose_activity_flex_ledger(token=TOKEN, query_id="12345", expected_account_ids=(ACCOUNT,),
            session=session, collect_pending=True, execution_source_only=True, report_period=PERIOD)
    assert len(session.calls) == 2


def configure(monkeypatch):
    for key in ("IBKR_FLEX_CASHFLOW_REQUEST_FROM", "IBKR_FLEX_CASHFLOW_REQUEST_TO",
                "IBKR_ACCOUNT_FACTS_ACCOUNT_SELECTOR_JSON", "IBKR_ACCOUNT_FACTS_ADDITIONAL_TARGETS_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IBKR_FLEX_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setenv("IBKR_FLEX_TOKEN", TOKEN)
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "12345")
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_ACCOUNT_IDS_JSON", json.dumps([ACCOUNT]))
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_FROM", PERIOD["from"])
    monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_TO", PERIOD["to"])


def test_cli_preflight_is_network_free_and_read_is_explicit(monkeypatch, capsys):
    configure(monkeypatch)
    assert cli.main(("--preflight", "--execution-source-only")) == 0
    capsys.readouterr()
    called = []
    def diagnose(**kwargs):
        called.append(kwargs)
        return diagnostic(trade(), trade(ibExecID="another.id"))
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", diagnose)
    assert cli.main(("--execution-read-once",)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["report_evidence"] == "newly_generated_report"
    assert called[0]["execution_source_only"] is True
    assert called[0]["report_period"] == PERIOD


@pytest.mark.parametrize("bad", ["missing", "too_long", "cashflow", "conflict", "disabled"])
def test_cli_bad_modes_or_window_reject_before_network(monkeypatch, capsys, bad):
    configure(monkeypatch)
    args = ("--execution-read-once",)
    if bad == "missing":
        monkeypatch.delenv("IBKR_FLEX_EXPECTED_PERIOD_TO")
    elif bad == "too_long":
        monkeypatch.setenv("IBKR_FLEX_EXPECTED_PERIOD_FROM", "2026-09-01")
    elif bad == "cashflow":
        monkeypatch.setenv("IBKR_FLEX_CASHFLOW_REQUEST_FROM", PERIOD["from"])
        monkeypatch.setenv("IBKR_FLEX_CASHFLOW_REQUEST_TO", PERIOD["to"])
    elif bad == "conflict":
        args += ("--cash-read-once",)
    else:
        monkeypatch.delenv("IBKR_FLEX_DIAGNOSTIC_ENABLED")
    assert cli.main(args) == 2
    output = capsys.readouterr().out
    assert TOKEN not in output and ACCOUNT not in output


def test_cli_validator_strips_unknown_keys_and_rejects_malformed_values():
    result = diagnostic(trade(), trade())
    result["private-xml-key"] = "private-value"
    result["economic_similarity_groups"][0]["private-xml-key"] = "private-value"
    assert "private" not in json.dumps(cli._validated_execution_report(result))
    result["economic_similarity_groups"][0]["comparison_sha256"] = "private-value"
    assert cli._validated_execution_report(result)["status"] == "error"


def test_workflow_new_mode_remains_default_closed_and_mutually_exclusive():
    workflow = Path(".github/workflows/diagnose-ibkr-flex-readonly.yml").read_text()
    assert 'execution_source_only:\n' in workflow
    assert '"$EXECUTION_SOURCE_ONLY"; do' in workflow
    assert 'preflight_args+=(--execution-source-only)' in workflow
    assert 'diagnose_ibkr_flex_ledger --execution-read-once' in workflow
    assert 'github.run_attempt == 1 && inputs.read_once' in workflow
    assert 'group: ibkr-flex-period-return' in workflow


def test_fee_comparison_preserves_all_native_decimal_digits():
    result = diagnostic(trade(ibCommission="-123456789012.123456789012345678"),
                        trade(ibExecID="another.id", ibCommission="-123456789012.123456789012345679"))
    assert result["economic_similarity_groups"][0]["fee_comparison"] == "different"


@pytest.mark.parametrize("endpoint,code", [("SendRequest", "1014"), ("GetStatement", "1012"), ("GetStatement", "1018")])
def test_cli_retains_safe_permanent_provider_error_without_retries(monkeypatch, capsys, endpoint, code):
    configure(monkeypatch)
    monkeypatch.setattr(source.time, "sleep", lambda _: None)
    error = f"<FlexStatementResponse><Status>Fail</Status><ErrorCode>{code}</ErrorCode><ErrorMessage>{TOKEN}/{REFERENCE}/{ACCOUNT}/private-body</ErrorMessage></FlexStatementResponse>".encode()
    session = Session(error) if endpoint == "SendRequest" else Session(SEND, error)
    def diagnose(**kwargs):
        return source.diagnose_activity_flex_ledger(session=session, **kwargs)
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", diagnose)
    assert cli.main(("--execution-read-once",)) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["missing"] == ["flex_import_failed"]
    assert result["source_endpoint"] == endpoint
    assert result["source_failure_kind"] == "provider"
    assert result["provider_error_code"] == code
    assert result["failure_stage"] == ("send_request" if endpoint == "SendRequest" else "get_statement")
    assert len(session.calls) == (1 if endpoint == "SendRequest" else 2)
    for private in (TOKEN, REFERENCE, ACCOUNT, "private-body", "FlexSourceError"):
        assert private not in output


def test_safe_error_projection_rejects_malicious_fields_and_types():
    private = "private-token/amount/account"
    output = cli._safe_source_failure({"source_endpoint": private, "source_failure_kind": private,
        "provider_error_code": private, "http_status": True, "reported_account_count": True,
        "unknown_account_count": -1, "target_accounts_present": private, "nested_account_mismatch": 1,
        "failure_stage": private, "private-key": private, "raw_xml": private})
    assert output == {}
    assert cli._safe_source_failure({"source_endpoint": "GetStatement", "source_failure_kind": []}) == {
        "source_endpoint": "GetStatement"}
    assert cli._safe_source_failure(None) == {}
    assert cli._safe_source_failure({"source_endpoint": "GetStatement", "source_failure_kind": "scope",
        "reported_account_count": 4, "target_accounts_present": False, "nested_account_mismatch": True,
        "raw_xml": private}) == {"source_endpoint": "GetStatement", "source_failure_kind": "scope",
        "reported_account_count": 4, "target_accounts_present": False, "nested_account_mismatch": True,
        "failure_stage": "verify_report"}
    assert cli._safe_source_failure({"source_endpoint": "SendRequest", "source_failure_kind": "http",
        "http_status": 503}) == {"source_endpoint": "SendRequest", "source_failure_kind": "http",
        "http_status": 503, "failure_stage": "send_request"}


@pytest.mark.parametrize("code", ["execution_source_period_invalid", "execution_source_rows_unsupported",
    "execution_source_record_limit", "execution_source_account_mismatch", "execution_source_date_invalid",
    "execution_source_period_mismatch"])
def test_cli_keeps_only_six_literal_execution_errors(monkeypatch, capsys, code):
    from application.ibkr_flex_executions import FlexExecutionsError
    configure(monkeypatch)
    def fail(**_):
        raise FlexExecutionsError(code)
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", fail)
    assert cli.main(("--execution-read-once",)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["missing"] == [code]
    assert result["failure_stage"] == ("verify_period" if code == "execution_source_period_mismatch" else "execution_shape")


@pytest.mark.parametrize("kind", ["runtime", "execution", "source"])
def test_cli_unknown_errors_never_print_exception_text_type_or_handle(monkeypatch, capsys, kind):
    from application.ibkr_flex_executions import FlexExecutionsError
    configure(monkeypatch)
    def fail(**_):
        text = f"private-error:{TOKEN}:{REFERENCE}:{ACCOUNT}"
        if kind == "source":
            error = source.FlexSourceError(text)
            error.safe_diagnostic = {"unexpected-field": text, "failure_stage": text, "provider_error_code": text}
            raise error
        raise (FlexExecutionsError(text) if kind == "execution" else RuntimeError(text))
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger", fail)
    assert cli.main(("--execution-read-once",)) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["missing"] == ["flex_import_failed"] and "failure_stage" not in result
    for private in (TOKEN, REFERENCE, ACCOUNT, "private-error", "RuntimeError", "FlexExecutionsError", "FlexSourceError"):
        assert private not in output


def test_cli_retains_actual_report_period_rejection_stage(monkeypatch, capsys):
    configure(monkeypatch)
    monkeypatch.setattr(source.time, "sleep", lambda _: None)
    session = Session(SEND, report(trade()).replace(b'toDate="20261027"', b'toDate="20261026"'))
    monkeypatch.setattr(cli, "diagnose_activity_flex_ledger",
                        lambda **kwargs: source.diagnose_activity_flex_ledger(session=session, **kwargs))
    assert cli.main(("--execution-read-once",)) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["missing"] == ["execution_source_period_mismatch"]
    assert result["failure_stage"] == "verify_period"
    assert len(session.calls) == 2
