from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _skip_if_missing_pandas() -> None:
    try:
        import pandas  # noqa: F401
    except ModuleNotFoundError as exc:
        raise unittest.SkipTest(f"pandas unavailable: {exc}") from exc


class FeatureSnapshotServiceTest(unittest.TestCase):
    def test_load_feature_snapshot_reads_csv(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot

        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "snapshot.csv"
            path.write_text("symbol,sector,mom_6_1\nAAA,tech,0.1\n", encoding="utf-8")

            frame = load_feature_snapshot(str(path))

        self.assertEqual(frame.to_dict(orient="records"), [{"symbol": "AAA", "sector": "tech", "mom_6_1": 0.1}])

    def test_load_feature_snapshot_rejects_unknown_format(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot

        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "snapshot.txt"
            path.write_text("hello", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Unsupported feature snapshot format"):
                load_feature_snapshot(str(path))

    def test_load_feature_snapshot_guarded_fails_closed_when_missing(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot_guarded

        result = load_feature_snapshot_guarded(
            "/tmp/definitely-missing-snapshot.csv",
            run_as_of="2026-04-05",
            required_columns=("symbol", "as_of"),
        )

        self.assertIsNone(result.frame)
        self.assertEqual(result.metadata["snapshot_guard_decision"], "fail_closed")
        self.assertIn("feature_snapshot_missing", result.metadata["fail_reason"])

    def test_load_feature_snapshot_guarded_fails_closed_when_stale(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot_guarded

        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "snapshot.csv"
            path.write_text("as_of,symbol,sector,mom_6_1\n2026-01-31,AAA,tech,0.1\n", encoding="utf-8")

            result = load_feature_snapshot_guarded(
                str(path),
                run_as_of="2026-04-05",
                required_columns=("as_of", "symbol", "sector", "mom_6_1"),
            )

        self.assertIsNone(result.frame)
        self.assertEqual(result.metadata["snapshot_guard_decision"], "fail_closed")
        self.assertIn("feature_snapshot_stale", result.metadata["fail_reason"])

    def test_load_feature_snapshot_guarded_requires_manifest_when_requested(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot_guarded

        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "snapshot.csv"
            path.write_text("as_of,symbol,sector,mom_6_1\n2026-03-31,AAA,tech,0.1\n", encoding="utf-8")

            result = load_feature_snapshot_guarded(
                str(path),
                run_as_of="2026-04-01",
                required_columns=("as_of", "symbol", "sector", "mom_6_1"),
                require_manifest=True,
                expected_strategy_profile="qqq_tech_enhancement",
                expected_config_name="qqq_tech_enhancement",
                expected_contract_version="qqq_tech_enhancement.feature_snapshot.v1",
            )

        self.assertIsNone(result.frame)
        self.assertEqual(result.metadata["snapshot_guard_decision"], "fail_closed")
        self.assertIn("feature_snapshot_manifest_missing", result.metadata["fail_reason"])

    def test_load_feature_snapshot_guarded_validates_manifest_checksums(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot_guarded

        with TemporaryDirectory() as tmp_dir:
            snapshot_path = Path(tmp_dir) / "snapshot.csv"
            config_path = Path(tmp_dir) / "config.json"
            snapshot_path.write_text("as_of,symbol,sector,mom_6_1\n2026-03-31,AAA,tech,0.1\n", encoding="utf-8")
            config_path.write_text(json.dumps({"name": "qqq_tech_enhancement"}), encoding="utf-8")
            manifest_path = Path(f"{snapshot_path}.manifest.json")
            manifest_path.write_text(
                json.dumps(
                    {
                        "contract_version": "qqq_tech_enhancement.feature_snapshot.v1",
                        "strategy_profile": "qqq_tech_enhancement",
                        "config_name": "qqq_tech_enhancement",
                        "config_path": str(config_path),
                        "config_sha256": _sha256_file(config_path),
                        "snapshot_path": str(snapshot_path),
                        "snapshot_sha256": _sha256_file(snapshot_path),
                        "snapshot_as_of": "2026-03-31",
                    }
                ),
                encoding="utf-8",
            )

            result = load_feature_snapshot_guarded(
                str(snapshot_path),
                run_as_of="2026-04-01",
                required_columns=("as_of", "symbol", "sector", "mom_6_1"),
                require_manifest=True,
                expected_strategy_profile="qqq_tech_enhancement",
                expected_config_name="qqq_tech_enhancement",
                expected_config_path=str(config_path),
                expected_contract_version="qqq_tech_enhancement.feature_snapshot.v1",
            )

        self.assertIsNotNone(result.frame)
        self.assertEqual(result.metadata["snapshot_guard_decision"], "proceed")
        self.assertEqual(result.metadata["snapshot_manifest_strategy_profile"], "qqq_tech_enhancement")
        self.assertEqual(result.metadata["snapshot_manifest_config_name"], "qqq_tech_enhancement")

    def test_load_feature_snapshot_downloads_gcs_csv(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot

        with TemporaryDirectory():
            source_uri = "gs://unit-test-bucket/snapshots/tech_pullback.csv"

            def fake_download(uri: str, destination: Path) -> None:
                self.assertEqual(uri, source_uri)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("symbol,sector,mom_6_1\nAAA,tech,0.1\n", encoding="utf-8")

            with patch("application.feature_snapshot_service._download_gcs_object", side_effect=fake_download):
                frame = load_feature_snapshot(source_uri)

        self.assertEqual(frame.to_dict(orient="records"), [{"symbol": "AAA", "sector": "tech", "mom_6_1": 0.1}])

    def test_load_feature_snapshot_guarded_downloads_gcs_snapshot_and_manifest(self):
        _skip_if_missing_pandas()
        from application.feature_snapshot_service import load_feature_snapshot_guarded

        with TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(json.dumps({"name": "qqq_tech_enhancement"}), encoding="utf-8")

            snapshot_uri = "gs://unit-test-bucket/snapshots/qqq_tech_enhancement_feature_snapshot_latest.csv"
            manifest_uri = f"{snapshot_uri}.manifest.json"
            downloaded_snapshot_path: Path | None = None

            def fake_download(uri: str, destination: Path) -> None:
                nonlocal downloaded_snapshot_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if uri == snapshot_uri:
                    downloaded_snapshot_path = destination
                    destination.write_text(
                        "as_of,symbol,sector,mom_6_1\n2026-03-31,AAA,Information Technology,0.1\n",
                        encoding="utf-8",
                    )
                    return
                if uri == manifest_uri:
                    self.assertIsNotNone(downloaded_snapshot_path)
                    destination.write_text(
                        json.dumps(
                            {
                                "contract_version": "qqq_tech_enhancement.feature_snapshot.v1",
                                "strategy_profile": "qqq_tech_enhancement",
                                "config_name": "qqq_tech_enhancement",
                                "config_path": str(config_path),
                                "config_sha256": _sha256_file(config_path),
                                "snapshot_path": snapshot_uri,
                                "snapshot_sha256": _sha256_file(downloaded_snapshot_path),
                                "snapshot_as_of": "2026-03-31",
                            }
                        ),
                        encoding="utf-8",
                    )
                    return
                raise AssertionError(f"unexpected uri: {uri}")

            with patch("application.feature_snapshot_service._download_gcs_object", side_effect=fake_download):
                result = load_feature_snapshot_guarded(
                    snapshot_uri,
                    run_as_of="2026-04-01",
                    required_columns=("as_of", "symbol", "sector", "mom_6_1"),
                    require_manifest=True,
                    expected_strategy_profile="qqq_tech_enhancement",
                    expected_config_name="qqq_tech_enhancement",
                    expected_config_path=str(config_path),
                    expected_contract_version="qqq_tech_enhancement.feature_snapshot.v1",
                )

        self.assertIsNotNone(result.frame)
        self.assertEqual(result.metadata["snapshot_guard_decision"], "proceed")
        self.assertEqual(
            result.metadata["feature_snapshot_path"],
            snapshot_uri,
        )
        self.assertEqual(result.metadata["snapshot_manifest_path"], manifest_uri)
        self.assertEqual(result.metadata["snapshot_source_uri"], snapshot_uri)
        self.assertEqual(result.metadata["snapshot_manifest_source_uri"], manifest_uri)


if __name__ == "__main__":
    unittest.main()
