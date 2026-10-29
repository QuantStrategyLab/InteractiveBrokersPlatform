"""Validate continuity and immutable traffic candidates without broker calls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def verify_target_continuity(target: Mapping[str, Any]) -> str:
    """Compute the QPK digest and check an existing frozen baseline; never rebase."""
    from quant_platform_kit.common.live_continuity import (
        build_live_continuity,
        runtime_target_fingerprint,
    )

    try:
        if target.get("live_continuity") is not None:
            build_live_continuity(target["live_continuity"]).assert_matches_target(target)
        return runtime_target_fingerprint(target)
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime target continuity validation failed") from exc


def verify_traffic_revision(
    revision: Mapping[str, Any], *, expected_revision: str, expected_commit: str | None,
) -> None:
    """Admit healthy candidates, or cold retired ones with execution disabled.

    A cold disabled candidate still needs a real health read after activation;
    its platform readiness is not evidence that application startup ran.
    """
    metadata = revision.get("metadata") or {}
    if metadata.get("name") != expected_revision or (metadata.get("labels") or {}).get("commit-sha") != expected_commit:
        raise ValueError("traffic candidate revision identity does not match")
    rows = revision.get("status", {}).get("conditions", [])
    conditions = {
        item.get("type"): item.get("status")
        for item in rows
    }
    containers = revision.get("spec", {}).get("containers") or []
    if not containers:
        raise ValueError("traffic candidate has no application container")
    container = containers[0]
    env = {
        item["name"]: item["value"]
        for item in container.get("env", [])
        if "value" in item
    }
    retired_disabled = (
        str(env.get("RUNTIME_TARGET_ENABLED", "")).strip().lower() == "false"
        and "ContainerHealthy" not in conditions
        and conditions.get("ContainerReady") == "True"
        and conditions.get("Active") == "False"
        and all(any(row.get("type") == kind and row.get("reason") == "Retired" for row in rows)
                for kind in ("Ready", "Active"))
    )
    if conditions.get("Ready") != "True" or not (
        conditions.get("ContainerHealthy") == "True" or retired_disabled
    ):
        raise ValueError("traffic candidate has not passed its application startup probe")
    probe = container.get("startupProbe") or {}
    http = probe.get("httpGet") or {}
    if http.get("path") != "/health" or http.get("port") != 8080 or "tcpSocket" in probe or "grpc" in probe:
        raise ValueError("traffic candidate requires an HTTP /health startup probe on port 8080")
    try:
        target = json.loads(env.get("RUNTIME_TARGET_JSON") or env.get("QSL_RUNTIME_TARGET_JSON") or "null")
    except (TypeError, ValueError) as exc:
        raise ValueError("traffic candidate runtime target is invalid") from exc
    if not isinstance(target, Mapping):
        raise ValueError("traffic candidate runtime target must be an object")
    verify_target_continuity(target)
