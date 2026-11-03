"""Synthetic privacy regressions for both notification output sinks."""
from copy import deepcopy

import pytest

import test_unified_notification_adapter as original
from notifications import unified_adapter as adapter
from notifications.events import NotificationPublisher, RenderedNotification

routing = original.routing


@pytest.mark.parametrize("flag", ["false", "true"])
@pytest.mark.parametrize("locale", ["en", "zh"])
@pytest.mark.parametrize("case,diagnostic,private", [
    ("host-test", "ConnectionError: Connect call failed ('gateway.synthetic.test', 4002)", "gateway.synthetic.test"),
    ("host-dev", "ConnectionError: Cannot connect to gateway.synthetic.dev:4002", "gateway.synthetic.dev"),
    ("project-resource", "PermissionDenied: projects/synthetic-project-z/topics/synthetic-topic", "synthetic-project-z"),
    ("host-ipv6", "ConnectionError: Connect call failed ('2001:db8::8', 4002)", "2001:db8::8"),
    ("host-new-suffix", "ConnectionError: gateway.synthetic.engineering refused", "gateway.synthetic.engineering"),
    ("host-punycode", "ConnectionError: gateway.xn--synthetic-9db refused", "gateway.xn--synthetic-9db"),
    ("host-trailing-dot", "ConnectionError: GATEWAY.SYNTHETIC.DEV. refused", "GATEWAY.SYNTHETIC.DEV"),
    ("ipv6-brackets", "ConnectionError: [2001:db8:1::9]:4002 refused", "2001:db8:1::9"),
    ("ipv6-full", "ConnectionError: 2001:0db8:0000:0000:0000:0000:0000:0009 refused", "2001:0db8:0000:0000:0000:0000:0000:0009"),
    ("ipv6-loopback", "ConnectionError: [::1]:4002 refused", "::1"),
    ("ipv6-zone", "ConnectionError: [fe80::9%synthetic0]:4002 refused", "fe80::9%synthetic0"),
    ("ipv6-mapped", "ConnectionError: [::ffff:192.0.2.9]:4002 refused", "::ffff:192.0.2.9"),
    ("project-number", "PermissionDenied: projects/123456789012/topics/synthetic-topic", "123456789012"),
    ("project-descendant", "PermissionDenied: projects/synthetic-project-z/topics/synthetic-topic", "synthetic-topic"),
])
def test_diagnostics_have_no_private_locations(routing, monkeypatch, flag, locale, case, diagnostic, private):
    monkeypatch.setenv(adapter.FLAG, flag)
    monkeypatch.setenv("QSL_NOTIFY_LANG", locale)
    sent, logs = [], []
    publisher = NotificationPublisher(log_message=logs.append, send_message=lambda text: sent.append(text) or True)
    summary = {"execution_status": "no_action", "fail_reason": diagnostic}
    saved = deepcopy(summary)
    adapter.with_run(original.report(), adapter.publish_cycle, publisher,
        notification=RenderedNotification("legacy", "legacy"), execution=summary,
        heartbeat=True, heartbeat_diagnostics=diagnostic + "; rejected:too_many_positions")
    assert summary == saved
    assert len(sent) == 1 and len(logs) == 1 and sent[0] == logs[0]
    assert all("rejected:too_many_positions" in body for body in [*sent, *logs])
    assert all(diagnostic.split(":", 1)[0] in body for body in [*sent, *logs])
    assert all(private not in body for body in [*sent, *logs]), "identity escaped to notification/log sinks: " + case


@pytest.mark.parametrize("flag", ["false", "true"])
@pytest.mark.parametrize("boundary", ["diagnostic", "final"])
def test_redactor_exception_blocks_all_sinks(routing, monkeypatch, flag, boundary):
    from notifications import redaction
    from quant_platform_kit.notifications import _redaction
    monkeypatch.setenv(adapter.FLAG, flag)
    sent, logs = [], []
    publisher = NotificationPublisher(log_message=logs.append, send_message=lambda text: sent.append(text) or True)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic_redactor_unavailable")
    if boundary == "diagnostic":
        monkeypatch.setattr(redaction, "redact_notification_diagnostics", fail)
    else:
        monkeypatch.setattr(_redaction, "redact_sensitive_text", fail)
    with pytest.raises(RuntimeError, match="synthetic_redactor_unavailable"):
        adapter.with_run(original.report(), adapter.publish_cycle, publisher,
            notification=RenderedNotification("unredacted", "unredacted"),
            execution={"execution_status": "no_action", "fail_reason": "synthetic failure"},
            heartbeat=True, heartbeat_diagnostics="synthetic failure")
    assert not sent and not logs
