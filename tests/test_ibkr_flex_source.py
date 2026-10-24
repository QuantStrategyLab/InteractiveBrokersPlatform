import pytest

from application import ibkr_flex_source as source


REFERENCE = "987654321098765"
TOKEN = "synthetic-private-flex-token"
SEND = f"<FlexStatementResponse><Status>Success</Status><ReferenceCode>{REFERENCE}</ReferenceCode></FlexStatementResponse>".encode()
PENDING = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode><ErrorMessage>synthetic-private-message</ErrorMessage></FlexStatementResponse>"
XML = b'<FlexQueryResponse type="AF"><FlexStatements><FlexStatement accountId="DU0000000" fromDate="20260101" toDate="20260131"><AccountInformation currency="EUR"/><ChangeInNAV fromDate="20260101" toDate="20260131" twr="1.25"/></FlexStatement></FlexStatements></FlexQueryResponse>'


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def iter_content(self, **_kwargs):
        yield self.body


class Session:
    def __init__(self, *bodies):
        self.bodies = iter(bodies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url.rsplit("/", 1)[-1], kwargs["params"]))
        assert kwargs["allow_redirects"] is False
        return Response(next(self.bodies))


@pytest.mark.parametrize("status", ["Fail", "Warn", None])
def test_1019_preserves_same_reference_in_safe_pending_exception(status):
    status_xml = f"<Status>{status}</Status>" if status else ""
    response = f"<FlexStatementResponse>{status_xml}<ErrorCode>1019</ErrorCode><ErrorMessage>synthetic-private-message</ErrorMessage></FlexStatementResponse>".encode()
    session = Session(SEND, response)
    with pytest.raises(source.FlexSourceError) as caught:
        source.fetch_activity_flex_xml(token=TOKEN, query_id="12345", session=session)
    pending = caught.value
    assert type(pending).__name__ == "FlexReportPending"
    assert pending.request.reference_code == REFERENCE
    assert len(session.calls) == 2
    assert session.calls[-1] == ("GetStatement", {"t": TOKEN, "q": REFERENCE, "v": "3"})
    for private in (REFERENCE, TOKEN, "synthetic-private-message"):
        assert private not in str(pending) + repr(pending) + repr(pending.request)


def test_explicit_collection_reuses_same_handle_without_new_generation():
    session = Session(SEND, PENDING, PENDING, XML)
    request = source.request_activity_flex_report(token=TOKEN, query_id="12345", session=session)
    assert len(session.calls) == 1
    for _ in range(2):
        with pytest.raises(source.FlexReportPending) as caught:
            source.collect_activity_flex_xml(request, session=session)
        assert caught.value.request is request
    ledger = source.import_activity_flex_ledger_from_request(
        request, expected_account_ids=("DU0000000",), session=session,
    )
    assert ledger["native_returns"]["twr"] == "1.25"
    assert [name for name, _ in session.calls] == ["SendRequest", "GetStatement", "GetStatement", "GetStatement"]
    assert all(params["q"] == REFERENCE and params["t"] == TOKEN for _, params in session.calls[1:])


@pytest.mark.parametrize("code", ["1012", "1017", "1018", "1021"])
def test_other_get_statement_errors_are_not_pending_and_never_retry(code):
    error = f"<FlexStatementResponse><Status>Fail</Status><ErrorCode>{code}</ErrorCode><ErrorMessage>{TOKEN}/{REFERENCE}</ErrorMessage></FlexStatementResponse>".encode()
    session = Session(SEND, error)
    with pytest.raises(source.FlexSourceError) as caught:
        source.fetch_activity_flex_xml(token=TOKEN, query_id="12345", session=session)
    assert type(caught.value) is source.FlexSourceError
    assert len(session.calls) == 2
    assert TOKEN not in str(caught.value) and REFERENCE not in repr(caught.value)


def test_send_rejection_does_not_produce_handle_or_collect():
    session = Session(PENDING)
    with pytest.raises(source.FlexSourceError) as caught:
        source.request_activity_flex_report(token=TOKEN, query_id="12345", session=session)
    assert type(caught.value) is source.FlexSourceError
    assert [name for name, _ in session.calls] == ["SendRequest"]


@pytest.mark.parametrize("code,expected", [("1017", "1017"), (TOKEN, None), ("999999", None)])
def test_provider_failure_has_only_fixed_diagnostic_fields(code, expected):
    response = f"<FlexStatementResponse><Status>Fail</Status><ErrorCode>{code}</ErrorCode><ErrorMessage>{TOKEN}</ErrorMessage></FlexStatementResponse>".encode()
    session = Session(response)
    with pytest.raises(source.FlexSourceError) as caught:
        source.request_activity_flex_report(token=TOKEN, query_id="12345", session=session)
    diagnostic = caught.value.safe_diagnostic
    assert diagnostic == {"source_endpoint": "SendRequest", "source_failure_kind": "provider",
                          **({"provider_error_code": expected} if expected else {})}
    assert TOKEN not in str(diagnostic) and REFERENCE not in str(diagnostic)
    assert len(session.calls) == 1


def test_collect_rejects_unscoped_report_without_generating():
    session = Session(SEND, XML)
    request = source.request_activity_flex_report(token=TOKEN, query_id="12345", session=session)
    with pytest.raises(source.FlexSourceError, match="account scope"):
        source.import_activity_flex_ledger_from_request(request, expected_account_ids=("DU0000004",), session=session)
    assert [name for name, _ in session.calls] == ["SendRequest", "GetStatement"]


def test_optional_date_override_preserves_token_and_single_generation():
    session = Session(SEND)
    source.request_activity_flex_report(token=TOKEN, query_id="12345", session=session,
        from_date="2026-01-19", to_date="2026-01-19")
    assert session.calls == [("SendRequest", {"t": TOKEN, "q": "12345", "v": "3", "fd": "20260119", "td": "20260119"})]


@pytest.mark.parametrize("start,end", [("2026-02-30", "2026-03-01"), ("2026-01-20", "2026-01-19"), (None, "2026-01-19"), ("2026-01-19", None), ("2024-01-01", "2026-01-01")])
def test_bad_date_override_is_rejected_before_io(start, end):
    session = Session()
    with pytest.raises(source.FlexSourceError):
        source.request_activity_flex_report(token=TOKEN, query_id="12345", session=session, from_date=start, to_date=end)
    assert session.calls == []
