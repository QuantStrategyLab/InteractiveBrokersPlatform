"""Resolve the console origin without embedding a deployment identifier."""

import os
from urllib.parse import urlsplit


def console_base_url() -> str:
    value = os.environ.get("QSL_CONSOLE_BASE_URL", "https://example.invalid").rstrip("/")
    endpoint = urlsplit(value)
    if (
        endpoint.scheme != "https" or not endpoint.hostname
        or endpoint.username is not None or endpoint.password is not None
        or endpoint.path or endpoint.query or endpoint.fragment
    ):
        raise ValueError("QSL_CONSOLE_BASE_URL must be an HTTPS origin")
    return value
