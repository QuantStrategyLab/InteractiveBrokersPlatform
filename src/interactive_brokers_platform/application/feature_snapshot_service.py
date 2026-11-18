"""Feature snapshot compatibility wrapper for InteractiveBrokersPlatform."""

from quant_platform_kit.common import feature_snapshot as _shared_feature_snapshot

DEFAULT_ARTIFACT_CACHE_DIR = _shared_feature_snapshot.DEFAULT_ARTIFACT_CACHE_DIR
DEFAULT_MAX_SNAPSHOT_MONTH_LAG = _shared_feature_snapshot.DEFAULT_MAX_SNAPSHOT_MONTH_LAG
DEFAULT_SNAPSHOT_DATE_COLUMNS = _shared_feature_snapshot.DEFAULT_SNAPSHOT_DATE_COLUMNS
DEFAULT_SNAPSHOT_MANIFEST_SUFFIX = _shared_feature_snapshot.DEFAULT_SNAPSHOT_MANIFEST_SUFFIX
FeatureSnapshotGuardResult = _shared_feature_snapshot.FeatureSnapshotGuardResult
_download_gcs_object = _shared_feature_snapshot._download_gcs_object


def _with_shared_download_override(func, *args, **kwargs):
    original = _shared_feature_snapshot._download_gcs_object
    _shared_feature_snapshot._download_gcs_object = _download_gcs_object
    try:
        return func(*args, **kwargs)
    finally:
        _shared_feature_snapshot._download_gcs_object = original


def load_feature_snapshot(path: str):
    return _with_shared_download_override(
        _shared_feature_snapshot.load_feature_snapshot,
        path,
    )


def load_feature_snapshot_guarded(path: str, **kwargs):
    return _with_shared_download_override(
        _shared_feature_snapshot.load_feature_snapshot_guarded,
        path,
        **kwargs,
    )

__all__ = [
    "DEFAULT_ARTIFACT_CACHE_DIR",
    "DEFAULT_MAX_SNAPSHOT_MONTH_LAG",
    "DEFAULT_SNAPSHOT_DATE_COLUMNS",
    "DEFAULT_SNAPSHOT_MANIFEST_SUFFIX",
    "FeatureSnapshotGuardResult",
    "_download_gcs_object",
    "load_feature_snapshot",
    "load_feature_snapshot_guarded",
]
