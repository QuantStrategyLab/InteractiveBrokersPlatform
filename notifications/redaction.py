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
# Match DNS labels independently of a suffix registry, including IDN/punycode.
# Conservative over-redaction of dotted names is preferable to leaking endpoints.
_HOST = re.compile(r"(?i)\b(?:[\w-]+\.)+[^\W\d_][\w-]*\.?")
_RESOURCE = re.compile(r"(?i)\bprojects/[^\s<>\"',;)\]}]+")
# Treat colon-delimited address-like tokens as private, even if malformed. Keep
# zone identifiers and IPv4-mapped tails inside the same replacement.
_IPV6 = re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?:%[\w.-]+)?")
_ACCOUNT = re.compile(r"(?i)\b(?:DU|U)\d{5,}\b")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def redact_notification_diagnostics(value: object) -> str:
    """Preserve failure details while removing credentials and identity bindings."""
    text = redact_sensitive_text(value)
    text = _BINDING.sub(lambda match: match.group(1) + "<redacted>", text)
    for pattern in (_LOCATION, _RESOURCE, _IPV6, _HOST, _ACCOUNT, _IP):
        text = pattern.sub("<redacted>", text)
    return text
