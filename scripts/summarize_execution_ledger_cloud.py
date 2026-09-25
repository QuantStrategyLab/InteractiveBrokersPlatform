#!/usr/bin/env python3
"""Print only aggregate IBKR execution-record types on an authorized cloud runner."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from urllib.parse import urlsplit


def _gcloud(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["gcloud", *args], capture_output=True, check=False)


def _segment(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9._=-]+", "-", str(value or "").strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-.")
    if not normalized:
        raise ValueError("missing execution scope")
    return normalized


def _scoped_prefixes(*, service: str, project: str, region: str) -> dict[str, str]:
    result = _gcloud(
        "run", "services", "describe", service,
        "--project", project, "--region", region, "--format=json",
    )
    if result.returncode:
        raise RuntimeError("Cloud Run service configuration is unavailable")
    payload = json.loads(result.stdout)
    env = {
        item["name"]: item.get("value")
        for container in payload["spec"]["template"]["spec"]["containers"]
        for item in container.get("env", ())
    }
    base = next(
        (
            env.get(name)
            for name in (
                "IBKR_EXECUTION_STATE_CLOUD_URI",
                "IBKR_EXECUTION_STATE_GCS_URI",
                "QSL_EXECUTION_REPORT_CLOUD_URI",
                "QSL_EXECUTION_REPORT_GCS_URI",
                "EXECUTION_REPORT_CLOUD_URI",
                "EXECUTION_REPORT_GCS_URI",
            )
            if env.get(name)
        ),
        None,
    )
    parsed = urlsplit(str(base or ""))
    if parsed.scheme != "gs" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("execution state cloud URI is unavailable")
    scope = "/".join(
        (
            "v1", "ibkr", _segment(env.get("ACCOUNT_GROUP")),
            _segment(env.get("STRATEGY_PROFILE")), "live",
        )
    )
    root = f"gs://{parsed.netloc}/{parsed.path.strip('/')}".rstrip("/")
    return {
        namespace: f"{root}/{namespace}/{scope}/"
        for namespace in ("execution_markers", "execution_outcomes")
    }


def _list_objects(prefix: str, *, project: str, limit: int) -> tuple[str, ...]:
    result = _gcloud("storage", "ls", "--recursive", prefix + "**", "--project", project)
    if result.returncode:
        if b"matched no objects" in result.stderr.lower() or b"no urls matched" in result.stderr.lower():
            return ()
        raise RuntimeError("execution state listing failed")
    uris = tuple(line.strip().decode("utf-8") for line in result.stdout.splitlines() if line.strip())
    if len(uris) > limit:
        raise RuntimeError("execution state exceeds bounded diagnostic limit")
    if any(not uri.startswith(prefix) or not uri.endswith(".json") for uri in uris):
        raise ValueError("execution state listing contains an invalid object")
    return uris


def _read_record(uri: str, *, prefix: str) -> tuple[str, str]:
    result = _gcloud("storage", "cat", uri)
    if result.returncode:
        raise RuntimeError("execution state record read failed")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("execution state record is invalid")
    marker_key = payload.get("marker_key")
    scoped_key_prefix = "/".join(prefix.rstrip("/").split("/")[-5:]) + "/"
    if (
        not isinstance(marker_key, str)
        or not marker_key.startswith(scoped_key_prefix)
        or uri != prefix + marker_key[len(scoped_key_prefix):] + ".json"
    ):
        raise ValueError("execution state record identity mismatch")
    return str(payload.get("schema_version") or ""), marker_key


def summarize(*, service: str, project: str, region: str, limit: int = 256) -> dict[str, object]:
    prefixes = _scoped_prefixes(service=service, project=project, region=region)
    records: dict[str, dict[str, str]] = {}
    for namespace, prefix in prefixes.items():
        records[namespace] = {}
        for uri in _list_objects(prefix, project=project, limit=limit):
            schema, marker_key = _read_record(uri, prefix=prefix)
            records[namespace][marker_key] = schema
    marker_schemas = Counter(records["execution_markers"].values())
    outcome_schemas = Counter(records["execution_outcomes"].values())
    if set(marker_schemas) - {"execution_claim.v1", "execution_marker.v1"}:
        raise ValueError("execution state contains an unsupported marker schema")
    if set(outcome_schemas) - {"execution_outcome.v1"}:
        raise ValueError("execution state contains an unsupported outcome schema")
    claims = {key for key, schema in records["execution_markers"].items() if schema == "execution_claim.v1"}
    outcomes = set(records["execution_outcomes"])
    return {
        "schema_version": "ibkr_execution_ledger_type_summary.v1",
        "bounded_complete": True,
        "claim_count": len(claims),
        "completion_marker_count": marker_schemas["execution_marker.v1"],
        "outcome_count": len(outcomes),
        "claim_without_outcome_count": len(claims - outcomes),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(service=args.service, project=args.project, region=args.region), sort_keys=True))
    except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"execution ledger diagnostic unavailable: {type(exc).__name__}") from None
