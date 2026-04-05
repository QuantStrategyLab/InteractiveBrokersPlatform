from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


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
                expected_strategy_profile="tech_pullback_cash_buffer",
                expected_config_name="tech_pullback_cash_buffer",
                expected_contract_version="tech_pullback_cash_buffer.feature_snapshot.v1",
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
            config_path.write_text(json.dumps({"name": "tech_pullback_cash_buffer"}), encoding="utf-8")
            manifest_path = Path(f"{snapshot_path}.manifest.json")
            manifest_path.write_text(
                json.dumps(
                    {
                        "contract_version": "tech_pullback_cash_buffer.feature_snapshot.v1",
                        "strategy_profile": "tech_pullback_cash_buffer",
                        "config_name": "tech_pullback_cash_buffer",
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
                expected_strategy_profile="tech_pullback_cash_buffer",
                expected_config_name="tech_pullback_cash_buffer",
                expected_config_path=str(config_path),
                expected_contract_version="tech_pullback_cash_buffer.feature_snapshot.v1",
            )

        self.assertIsNotNone(result.frame)
        self.assertEqual(result.metadata["snapshot_guard_decision"], "proceed")
        self.assertEqual(result.metadata["snapshot_manifest_strategy_profile"], "tech_pullback_cash_buffer")
        self.assertEqual(result.metadata["snapshot_manifest_config_name"], "tech_pullback_cash_buffer")


if __name__ == "__main__":
    unittest.main()
