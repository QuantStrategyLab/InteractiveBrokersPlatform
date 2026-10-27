#!/usr/bin/env python3
"""Print aggregate IBKR record types and unresolved-claim dates on an authorized runner."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from datetime import date, datetime, timezone
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


def _claim_date(value: object, *, timestamp: bool = False) -> str | None:
    """Keep unclassified dates unknown; never echo a private key or metadata."""
    if not isinstance(value, str):
        return None
    try:
        if timestamp:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(timezone.utc).date().isoformat() if parsed.tzinfo else None
        return date.fromisoformat(value).isoformat() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
    except ValueError:
        return None


def _read_record(uri: str, *, prefix: str) -> tuple[str, str, str | None]:
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
    return str(payload.get("schema_version") or ""), marker_key, _claim_date(payload.get("claimed_at"), timestamp=True)


def summarize(*, service: str, project: str, region: str, limit: int = 256) -> dict[str, object]:
    prefixes = _scoped_prefixes(service=service, project=project, region=region)
    records: dict[str, dict[str, str]] = {}
    claimed_dates: dict[str, str | None] = {}
    for namespace, prefix in prefixes.items():
        records[namespace] = {}
        for uri in _list_objects(prefix, project=project, limit=limit):
            schema, marker_key, claimed_date = _read_record(uri, prefix=prefix)
            records[namespace][marker_key] = schema
            if namespace == "execution_markers" and schema == "execution_claim.v1":
                claimed_dates[marker_key] = claimed_date
    marker_schemas = Counter(records["execution_markers"].values())
    outcome_schemas = Counter(records["execution_outcomes"].values())
    if set(marker_schemas) - {"execution_claim.v1", "execution_marker.v1"}:
        raise ValueError("execution state contains an unsupported marker schema")
    if set(outcome_schemas) - {"execution_outcome.v1"}:
        raise ValueError("execution state contains an unsupported outcome schema")
    claims = {key for key, schema in records["execution_markers"].items() if schema == "execution_claim.v1"}
    outcomes = set(records["execution_outcomes"])
    unresolved_dates = Counter()
    for key in claims - outcomes:
        parts = key.split("/")
        signal = _claim_date(parts[5]) if len(parts) == 8 else None
        effective = _claim_date(parts[6]) if len(parts) == 8 else None
        unresolved_dates[(signal, effective, claimed_dates[key])] += 1
    date_counts = [
        {"signal_date": signal, "effective_date": effective, "claimed_date_utc": claimed, "count": count}
        for (signal, effective, claimed), count in sorted(
            unresolved_dates.items(), key=lambda item: tuple(value or "" for value in item[0])
        )
    ]
    return {
        "schema_version": "ibkr_execution_ledger_type_summary.v1",
        "bounded_complete": True,
        "claim_count": len(claims),
        "completion_marker_count": marker_schemas["execution_marker.v1"],
        "outcome_count": len(outcomes),
        "claim_without_outcome_count": len(claims - outcomes),
        "claim_without_outcome_date_counts": date_counts,
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
