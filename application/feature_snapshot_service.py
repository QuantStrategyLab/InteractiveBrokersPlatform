"""Feature snapshot loading helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Iterable

import pandas as pd


DEFAULT_SNAPSHOT_DATE_COLUMNS = ("as_of", "snapshot_date")
DEFAULT_MAX_SNAPSHOT_MONTH_LAG = 1
DEFAULT_SNAPSHOT_MANIFEST_SUFFIX = ".manifest.json"


@dataclass(frozen=True)
class FeatureSnapshotGuardResult:
    frame: pd.DataFrame | None
    metadata: dict[str, object]


def _load_snapshot_frame(snapshot_path: Path) -> pd.DataFrame:
    suffix = snapshot_path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(snapshot_path)
    if suffix in {".json", ".jsonl"}:
        return pd.read_json(snapshot_path, orient="records", lines=suffix == ".jsonl")
    if suffix == ".parquet":
        return pd.read_parquet(snapshot_path)

    raise ValueError(
        "Unsupported feature snapshot format; expected .csv, .json, .jsonl, or .parquet"
    )


def _normalize_timestamp(value) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    else:
        ts = ts.tz_localize(None)
    return ts.normalize()


def _month_lag(snapshot_as_of: pd.Timestamp, run_as_of: pd.Timestamp) -> int:
    return (run_as_of.year - snapshot_as_of.year) * 12 + (run_as_of.month - snapshot_as_of.month)


def _build_guard_metadata(
    *,
    snapshot_path: Path,
    decision: str,
    snapshot_format: str | None = None,
    snapshot_exists: bool,
    snapshot_as_of: pd.Timestamp | None = None,
    file_timestamp: str | None = None,
    age_days: int | None = None,
    no_op_reason: str | None = None,
    fail_reason: str | None = None,
    **extra,
) -> dict[str, object]:
    payload = {
        "feature_snapshot_path": str(snapshot_path),
        "snapshot_path": str(snapshot_path),
        "snapshot_format": snapshot_format,
        "snapshot_exists": bool(snapshot_exists),
        "snapshot_as_of": snapshot_as_of,
        "snapshot_file_timestamp": file_timestamp,
        "snapshot_age_days": age_days,
        "snapshot_guard_decision": decision,
        "no_op_reason": no_op_reason,
        "fail_reason": fail_reason,
    }
    payload.update(extra)
    return payload


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _resolve_manifest_path(snapshot_path: Path, manifest_path: str | None) -> Path:
    raw_manifest = str(manifest_path or "").strip()
    if raw_manifest:
        return Path(raw_manifest)
    return Path(f"{snapshot_path}{DEFAULT_SNAPSHOT_MANIFEST_SUFFIX}")


def _normalize_manifest_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    if "snapshot_as_of" in normalized:
        normalized["snapshot_as_of"] = _normalize_timestamp(normalized.get("snapshot_as_of"))
    return normalized


def _load_manifest_payload(manifest_path: Path) -> dict[str, object]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("snapshot manifest must be a JSON object")
    return _normalize_manifest_payload(payload)


def load_feature_snapshot(path: str) -> pd.DataFrame:
    raw_path = str(path or "").strip()
    if not raw_path:
        raise EnvironmentError("Feature snapshot path is required")
    snapshot_path = Path(raw_path)
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Feature snapshot not found: {snapshot_path}")
    return _load_snapshot_frame(snapshot_path)


def load_feature_snapshot_guarded(
    path: str,
    *,
    run_as_of,
    required_columns: Iterable[str] | None = None,
    snapshot_date_columns: Iterable[str] = DEFAULT_SNAPSHOT_DATE_COLUMNS,
    max_snapshot_month_lag: int = DEFAULT_MAX_SNAPSHOT_MONTH_LAG,
    manifest_path: str | None = None,
    require_manifest: bool = False,
    expected_strategy_profile: str | None = None,
    expected_config_name: str | None = None,
    expected_config_path: str | None = None,
    expected_contract_version: str | None = None,
) -> FeatureSnapshotGuardResult:
    raw_path = str(path or "").strip()
    if not raw_path:
        snapshot_path = Path("<missing>")
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_exists=False,
                fail_reason="feature_snapshot_path_missing",
            ),
        )

    snapshot_path = Path(raw_path)
    manifest_file = _resolve_manifest_path(snapshot_path, manifest_path)
    file_timestamp = None
    if snapshot_path.exists():
        stat = snapshot_path.stat()
        file_timestamp = pd.Timestamp(stat.st_mtime, unit="s", tz=timezone.utc).isoformat()
    manifest_file_timestamp = None
    if manifest_file.exists():
        manifest_stat = manifest_file.stat()
        manifest_file_timestamp = pd.Timestamp(
            manifest_stat.st_mtime,
            unit="s",
            tz=timezone.utc,
        ).isoformat()

    if not snapshot_path.exists():
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_exists=False,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=False,
                fail_reason=f"feature_snapshot_missing:{snapshot_path}",
            ),
        )

    try:
        frame = _load_snapshot_frame(snapshot_path)
    except Exception as exc:  # pragma: no cover - exercised in tests through ValueError path
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_format=snapshot_path.suffix.lower() or None,
                snapshot_exists=True,
                file_timestamp=file_timestamp,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=manifest_file.exists(),
                snapshot_manifest_file_timestamp=manifest_file_timestamp,
                fail_reason=f"feature_snapshot_parse_failed:{type(exc).__name__}:{exc}",
            ),
        )

    if frame.empty:
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_format=snapshot_path.suffix.lower() or None,
                snapshot_exists=True,
                file_timestamp=file_timestamp,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=manifest_file.exists(),
                snapshot_manifest_file_timestamp=manifest_file_timestamp,
                fail_reason="feature_snapshot_empty",
            ),
        )

    required = {str(column) for column in (required_columns or ()) if str(column).strip()}
    missing_columns = required - set(frame.columns)
    if missing_columns:
        missing_text = ",".join(sorted(missing_columns))
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_format=snapshot_path.suffix.lower() or None,
                snapshot_exists=True,
                file_timestamp=file_timestamp,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=manifest_file.exists(),
                snapshot_manifest_file_timestamp=manifest_file_timestamp,
                fail_reason=f"feature_snapshot_missing_columns:{missing_text}",
            ),
        )

    date_columns = tuple(str(column) for column in snapshot_date_columns if str(column).strip())
    selected_date_column = next((column for column in date_columns if column in frame.columns), None)
    if selected_date_column is None:
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_format=snapshot_path.suffix.lower() or None,
                snapshot_exists=True,
                file_timestamp=file_timestamp,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=manifest_file.exists(),
                snapshot_manifest_file_timestamp=manifest_file_timestamp,
                fail_reason=f"feature_snapshot_missing_date_column:candidates={','.join(date_columns)}",
            ),
        )

    snapshot_dates = pd.to_datetime(frame[selected_date_column], errors="coerce", utc=False)
    if getattr(snapshot_dates.dt, "tz", None) is not None:
        snapshot_dates = snapshot_dates.dt.tz_localize(None)
    snapshot_dates = snapshot_dates.dt.normalize()
    if snapshot_dates.notna().sum() == 0:
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_format=snapshot_path.suffix.lower() or None,
                snapshot_exists=True,
                file_timestamp=file_timestamp,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=manifest_file.exists(),
                snapshot_manifest_file_timestamp=manifest_file_timestamp,
                fail_reason=f"feature_snapshot_invalid_date_column:{selected_date_column}",
            ),
        )

    snapshot_as_of = pd.Timestamp(snapshot_dates.max()).normalize()
    run_date = _normalize_timestamp(run_as_of)
    if run_date is None:
        raise ValueError("run_as_of is required for guarded feature snapshot loading")

    age_days = int((run_date - snapshot_as_of).days)
    if age_days < 0:
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_format=snapshot_path.suffix.lower() or None,
                snapshot_exists=True,
                snapshot_as_of=snapshot_as_of,
                file_timestamp=file_timestamp,
                age_days=age_days,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=manifest_file.exists(),
                snapshot_manifest_file_timestamp=manifest_file_timestamp,
                fail_reason=f"feature_snapshot_future_as_of:{snapshot_as_of.date()}",
            ),
        )

    if _month_lag(snapshot_as_of, run_date) > int(max_snapshot_month_lag):
        return FeatureSnapshotGuardResult(
            frame=None,
            metadata=_build_guard_metadata(
                snapshot_path=snapshot_path,
                decision="fail_closed",
                snapshot_format=snapshot_path.suffix.lower() or None,
                snapshot_exists=True,
                snapshot_as_of=snapshot_as_of,
                file_timestamp=file_timestamp,
                age_days=age_days,
                snapshot_manifest_path=str(manifest_file),
                snapshot_manifest_exists=manifest_file.exists(),
                snapshot_manifest_file_timestamp=manifest_file_timestamp,
                fail_reason=(
                    "feature_snapshot_stale:"
                    f"snapshot_as_of={snapshot_as_of.date()} run_as_of={run_date.date()} "
                    f"max_month_lag={int(max_snapshot_month_lag)}"
                ),
            ),
        )

    actual_snapshot_sha256 = None
    actual_config_sha256 = None
    manifest_payload: dict[str, object] | None = None
    manifest_exists = manifest_file.exists()
    if require_manifest:
        if not manifest_exists:
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=False,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    fail_reason=f"feature_snapshot_manifest_missing:{manifest_file}",
                ),
            )
        try:
            manifest_payload = _load_manifest_payload(manifest_file)
        except Exception as exc:
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=True,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    fail_reason=f"feature_snapshot_manifest_parse_failed:{type(exc).__name__}:{exc}",
                ),
            )

        required_manifest_fields = {
            "contract_version",
            "strategy_profile",
            "config_name",
            "snapshot_as_of",
            "snapshot_sha256",
            "config_sha256",
        }
        missing_manifest_fields = sorted(
            field for field in required_manifest_fields if not str(manifest_payload.get(field) or "").strip()
        )
        if missing_manifest_fields:
            missing_text = ",".join(missing_manifest_fields)
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=True,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    fail_reason=f"feature_snapshot_manifest_missing_fields:{missing_text}",
                ),
            )

        manifest_as_of = manifest_payload.get("snapshot_as_of")
        if manifest_as_of != snapshot_as_of:
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=True,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    snapshot_manifest_contract_version=manifest_payload.get("contract_version"),
                    snapshot_manifest_strategy_profile=manifest_payload.get("strategy_profile"),
                    snapshot_manifest_config_name=manifest_payload.get("config_name"),
                    fail_reason=(
                        "feature_snapshot_manifest_as_of_mismatch:"
                        f"manifest={manifest_as_of} snapshot={snapshot_as_of.date()}"
                    ),
                ),
            )

        if expected_strategy_profile and str(manifest_payload.get("strategy_profile")).strip() != str(expected_strategy_profile).strip():
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=True,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    snapshot_manifest_contract_version=manifest_payload.get("contract_version"),
                    snapshot_manifest_strategy_profile=manifest_payload.get("strategy_profile"),
                    snapshot_manifest_config_name=manifest_payload.get("config_name"),
                    fail_reason=(
                        "feature_snapshot_manifest_strategy_profile_mismatch:"
                        f"expected={expected_strategy_profile} actual={manifest_payload.get('strategy_profile')}"
                    ),
                ),
            )

        if expected_config_name and str(manifest_payload.get("config_name")).strip() != str(expected_config_name).strip():
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=True,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    snapshot_manifest_contract_version=manifest_payload.get("contract_version"),
                    snapshot_manifest_strategy_profile=manifest_payload.get("strategy_profile"),
                    snapshot_manifest_config_name=manifest_payload.get("config_name"),
                    fail_reason=(
                        "feature_snapshot_manifest_config_name_mismatch:"
                        f"expected={expected_config_name} actual={manifest_payload.get('config_name')}"
                    ),
                ),
            )

        if expected_contract_version and str(manifest_payload.get("contract_version")).strip() != str(expected_contract_version).strip():
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=True,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    snapshot_manifest_contract_version=manifest_payload.get("contract_version"),
                    snapshot_manifest_strategy_profile=manifest_payload.get("strategy_profile"),
                    snapshot_manifest_config_name=manifest_payload.get("config_name"),
                    fail_reason=(
                        "feature_snapshot_manifest_contract_version_mismatch:"
                        f"expected={expected_contract_version} actual={manifest_payload.get('contract_version')}"
                    ),
                ),
            )

        actual_snapshot_sha256 = _sha256_file(snapshot_path)
        if str(manifest_payload.get("snapshot_sha256")).strip() != actual_snapshot_sha256:
            return FeatureSnapshotGuardResult(
                frame=None,
                metadata=_build_guard_metadata(
                    snapshot_path=snapshot_path,
                    decision="fail_closed",
                    snapshot_format=snapshot_path.suffix.lower() or None,
                    snapshot_exists=True,
                    snapshot_as_of=snapshot_as_of,
                    file_timestamp=file_timestamp,
                    age_days=age_days,
                    snapshot_manifest_path=str(manifest_file),
                    snapshot_manifest_exists=True,
                    snapshot_manifest_file_timestamp=manifest_file_timestamp,
                    snapshot_manifest_contract_version=manifest_payload.get("contract_version"),
                    snapshot_manifest_strategy_profile=manifest_payload.get("strategy_profile"),
                    snapshot_manifest_config_name=manifest_payload.get("config_name"),
                    snapshot_manifest_snapshot_sha256=manifest_payload.get("snapshot_sha256"),
                    fail_reason="feature_snapshot_manifest_snapshot_checksum_mismatch",
                ),
            )

        if expected_config_path:
            config_file = Path(str(expected_config_path))
            if not config_file.exists():
                return FeatureSnapshotGuardResult(
                    frame=None,
                    metadata=_build_guard_metadata(
                        snapshot_path=snapshot_path,
                        decision="fail_closed",
                        snapshot_format=snapshot_path.suffix.lower() or None,
                        snapshot_exists=True,
                        snapshot_as_of=snapshot_as_of,
                        file_timestamp=file_timestamp,
                        age_days=age_days,
                        snapshot_manifest_path=str(manifest_file),
                        snapshot_manifest_exists=True,
                        snapshot_manifest_file_timestamp=manifest_file_timestamp,
                        snapshot_manifest_contract_version=manifest_payload.get("contract_version"),
                        snapshot_manifest_strategy_profile=manifest_payload.get("strategy_profile"),
                        snapshot_manifest_config_name=manifest_payload.get("config_name"),
                        fail_reason=f"feature_snapshot_expected_config_missing:{config_file}",
                    ),
                )
            actual_config_sha256 = _sha256_file(config_file)
            if str(manifest_payload.get("config_sha256")).strip() != actual_config_sha256:
                return FeatureSnapshotGuardResult(
                    frame=None,
                    metadata=_build_guard_metadata(
                        snapshot_path=snapshot_path,
                        decision="fail_closed",
                        snapshot_format=snapshot_path.suffix.lower() or None,
                        snapshot_exists=True,
                        snapshot_as_of=snapshot_as_of,
                        file_timestamp=file_timestamp,
                        age_days=age_days,
                        snapshot_manifest_path=str(manifest_file),
                        snapshot_manifest_exists=True,
                        snapshot_manifest_file_timestamp=manifest_file_timestamp,
                        snapshot_manifest_contract_version=manifest_payload.get("contract_version"),
                        snapshot_manifest_strategy_profile=manifest_payload.get("strategy_profile"),
                        snapshot_manifest_config_name=manifest_payload.get("config_name"),
                        snapshot_manifest_config_sha256=manifest_payload.get("config_sha256"),
                        fail_reason="feature_snapshot_manifest_config_checksum_mismatch",
                    ),
                )

    return FeatureSnapshotGuardResult(
        frame=frame,
        metadata=_build_guard_metadata(
            snapshot_path=snapshot_path,
            decision="proceed",
            snapshot_format=snapshot_path.suffix.lower() or None,
            snapshot_exists=True,
            snapshot_as_of=snapshot_as_of,
            file_timestamp=file_timestamp,
            age_days=age_days,
            snapshot_manifest_path=str(manifest_file),
            snapshot_manifest_exists=manifest_exists,
            snapshot_manifest_file_timestamp=manifest_file_timestamp,
            snapshot_manifest_contract_version=(manifest_payload or {}).get("contract_version"),
            snapshot_manifest_strategy_profile=(manifest_payload or {}).get("strategy_profile"),
            snapshot_manifest_config_name=(manifest_payload or {}).get("config_name"),
            snapshot_manifest_config_path=(manifest_payload or {}).get("config_path"),
            snapshot_manifest_snapshot_sha256=(manifest_payload or {}).get("snapshot_sha256"),
            snapshot_manifest_config_sha256=(manifest_payload or {}).get("config_sha256"),
            expected_strategy_profile=expected_strategy_profile,
            expected_config_name=expected_config_name,
            expected_config_path=expected_config_path,
            expected_contract_version=expected_contract_version,
            actual_snapshot_sha256=actual_snapshot_sha256,
            actual_config_sha256=actual_config_sha256,
        ),
    )
