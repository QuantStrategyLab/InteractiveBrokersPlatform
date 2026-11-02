"""Shared privacy boundary for notification decision diagnostics."""

from __future__ import annotations

import re

from quant_platform_kit.notifications._redaction import redact_sensitive_text

# QPK handles credentials. Diagnostics additionally carry broker/account labels,
# resource bindings and network locations; none are needed to explain a failure.
_BINDING = re.compile(
    r"(?i)(\b(?:account(?:[_ -]?(?:id|alias|name))?|project(?:[_ -]?id)?|"
    r"hostname|host|server)[\"']?\s*[:=]\s*)"
    r"(?:([\"'])(.*?)\2|([^\s,;}\]]+))"
)
_LOCATION = re.compile(r"(?i)https?://[^\s<>]+|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b")
_HOST = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|ai|internal|local|invalid)\b")
_ACCOUNT = re.compile(r"(?i)\b(?:DU|U)\d{5,}\b")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def redact_notification_diagnostics(value: object) -> str:
    """Preserve failure details while removing credentials and identity bindings."""
    text = redact_sensitive_text(value)
    text = _BINDING.sub(lambda match: match.group(1) + "<redacted>", text)
    for pattern in (_LOCATION, _HOST, _ACCOUNT, _IP):
        text = pattern.sub("<redacted>", text)
    return text
