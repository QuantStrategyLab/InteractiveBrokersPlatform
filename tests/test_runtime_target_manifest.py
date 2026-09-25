from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from application.runtime_target_manifest import (
    RuntimeTargetManifestError,
    default_manifest_path,
    iter_enabled_targets,
    load_runtime_target_manifest,
    validate_runtime_target_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "config" / "runtime_targets.manifest.json"

EXPECTED_LIVE_TARGETS = {
    "soxl_soxx_trend_income": {
        "label": "SOXL/SOXX trend income",
        "service": "interactive-brokers-quant-live-u00000001-service",
        "account_group": "live-u00000001",
        "strategy_profile": "soxl_soxx_trend_income",
    },
    "tqqq_growth_income": {
        "label": "TQQQ growth income",
        "service": "interactive-brokers-quant-live-u00000002-service",
        "account_group": "live-u00000002",
        "strategy_profile": "tqqq_growth_income",
    },
    "global_etf_rotation": {
        "label": "Global ETF rotation",
        "service": "interactive-brokers-quant-live-u00000003-service",
        "account_group": "live-u00000003",
        "strategy_profile": "global_etf_rotation",
    },
    "russell_top50_leader_rotation": {
        "label": "Russell top-50 leader rotation",
        "service": "interactive-brokers-quant-live-u00000004-service",
        "account_group": "live-u00000004",
        "strategy_profile": "russell_top50_leader_rotation",
    },
}


def _valid_payload() -> dict:
    return {
        "schema_version": 1,
        "platform_id": "ibkr",
        "targets": [
            {
                "id": "soxl_soxx_trend_income",
                "label": "SOXL/SOXX trend income",
                "service": "interactive-brokers-quant-live-u00000001-service",
                "region": "us-central1",
                "account_group": "live-u00000001",
                "strategy_profile": "soxl_soxx_trend_income",
                "execution_mode": "live",
                "lifecycle_role": "live",
                "enabled": False,
                "include_lifecycle": True,
                "include_reconciliation": True,
            }
        ],
    }


def test_default_manifest_path_points_at_checked_in_file():
    assert default_manifest_path() == MANIFEST_PATH
    assert MANIFEST_PATH.is_file()


def test_checked_in_manifest_parity_covers_four_live_and_us_combo_shadow():
    manifest = load_runtime_target_manifest()
    assert manifest.platform_id == "ibkr"
    assert manifest.schema_version == 1
    by_id = {target.id: target for target in manifest.targets}
    assert set(by_id) == set(EXPECTED_LIVE_TARGETS) | {"us_combo_shadow"}

    for target_id, expected in EXPECTED_LIVE_TARGETS.items():
        target = by_id[target_id]
        assert target.label == expected["label"]
        assert target.service == expected["service"]
        assert target.account_group == expected["account_group"]
        assert target.strategy_profile == expected["strategy_profile"]
        assert target.region == "us-central1"
        assert target.execution_mode == "live"
        assert target.lifecycle_role == "live"
        assert target.enabled is False
        assert target.include_lifecycle is True
        assert target.include_reconciliation is True

    shadow = by_id["us_combo_shadow"]
    assert shadow.label == "US combo shadow"
    assert shadow.service == "interactive-brokers-us-combo-shadow-service"
    assert shadow.account_group == "us-combo-shadow"
    assert shadow.strategy_profile == "tqqq_growth_income"
    assert shadow.region == "us-central1"
    assert shadow.execution_mode == "shadow"
    assert shadow.lifecycle_role == "shadow"
    assert shadow.enabled is False
    assert shadow.include_lifecycle is False
    assert shadow.include_reconciliation is False

    assert list(iter_enabled_targets(manifest)) == []


def test_missing_enabled_defaults_to_disabled():
    payload = _valid_payload()
    del payload["targets"][0]["enabled"]
    manifest = validate_runtime_target_manifest(payload)
    assert manifest.targets[0].enabled is False


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda p: p.update({"schema_version": 2}), "Unsupported schema_version"),
        (lambda p: p.update({"platform_id": "longbridge"}), "platform_id must be"),
        (lambda p: p.update({"targets": []}), "at least one entry"),
        (
            lambda p: p["targets"][0].update({"execution_mode": "dry-run"}),
            "execution_mode must be one of",
        ),
        (
            lambda p: p["targets"][0].update({"lifecycle_role": "observe"}),
            "lifecycle_role must be one of",
        ),
        (
            lambda p: p["targets"][0].update(
                {"execution_mode": "live", "lifecycle_role": "shadow"}
            ),
            "must match",
        ),
        (
            lambda p: p["targets"][0].pop("account_group"),
            "missing required fields: account_group",
        ),
        (
            lambda p: p["targets"][0].pop("include_reconciliation"),
            "missing required fields: include_reconciliation",
        ),
        (
            lambda p: p["targets"].append(copy.deepcopy(p["targets"][0])),
            "Duplicate target id",
        ),
        (
            lambda p: (
                p["targets"].append(
                    {
                        **copy.deepcopy(p["targets"][0]),
                        "id": "other",
                        "account_group": "live-other",
                    }
                )
            ),
            "Duplicate Cloud Run service",
        ),
        (
            lambda p: (
                p["targets"].append(
                    {
                        **copy.deepcopy(p["targets"][0]),
                        "id": "other",
                        "service": "interactive-brokers-other-service",
                    }
                )
            ),
            "Duplicate account_group",
        ),
        (
            lambda p: p["targets"][0].update({"account_ids": ["U123"]}),
            "must not contain sensitive field",
        ),
        (
            lambda p: p["targets"][0].update({"ib_gateway_port": 4001}),
            "must not contain sensitive field",
        ),
        (
            lambda p: p["targets"][0].update({"ib_client_id": 7}),
            "must not contain sensitive field",
        ),
        (
            lambda p: p["targets"][0].update({"token": "abc"}),
            "must not contain sensitive field",
        ),
        (
            lambda p: p["targets"][0].update({"continuity_fingerprint": "deadbeef"}),
            "must not contain sensitive field",
        ),
        (
            lambda p: p["targets"][0].update({"description": "Bearer " + ("x" * 40)}),
            "secret value",
        ),
        (
            lambda p: p["targets"][0].update({"enabled": "false"}),
            "enabled must be a boolean",
        ),
        (
            lambda p: p["targets"][0].update(
                {
                    "execution_mode": "shadow",
                    "lifecycle_role": "shadow",
                    "include_reconciliation": True,
                    "include_lifecycle": False,
                }
            ),
            "include_reconciliation may only be true when execution_mode is 'live'",
        ),
    ],
)
def test_manifest_rejects_invalid_payloads(mutator, message):
    payload = _valid_payload()
    mutator(payload)
    with pytest.raises(RuntimeTargetManifestError, match=message):
        validate_runtime_target_manifest(payload)


def test_manifest_accepts_live_paper_and_shadow_execution_modes():
    payload = _valid_payload()
    base = payload["targets"][0]
    payload["targets"] = [
        {
            **copy.deepcopy(base),
            "id": "a",
            "execution_mode": "live",
            "lifecycle_role": "live",
            "service": "svc-a",
            "account_group": "group-a",
            "include_reconciliation": True,
        },
        {
            **copy.deepcopy(base),
            "id": "b",
            "execution_mode": "paper",
            "lifecycle_role": "paper",
            "service": "svc-b",
            "account_group": "group-b",
            "include_lifecycle": False,
            "include_reconciliation": False,
        },
        {
            **copy.deepcopy(base),
            "id": "c",
            "execution_mode": "shadow",
            "lifecycle_role": "shadow",
            "service": "svc-c",
            "account_group": "group-c",
            "include_lifecycle": False,
            "include_reconciliation": False,
        },
    ]
    manifest = validate_runtime_target_manifest(payload)
    assert [target.execution_mode for target in manifest.targets] == [
        "live",
        "paper",
        "shadow",
    ]


def test_validate_script_accepts_checked_in_manifest(tmp_path, capsys):
    import importlib.util

    script_path = REPO_ROOT / "scripts" / "validate_runtime_target_manifest.py"
    spec = importlib.util.spec_from_file_location(
        "validate_runtime_target_manifest", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main([]) == 0
    out = capsys.readouterr().out
    assert "targets=5" in out
    assert module.main(["--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["target_count"] == 5
    assert {item["id"] for item in summary["targets"]} == set(EXPECTED_LIVE_TARGETS) | {
        "us_combo_shadow"
    }

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"schema_version": 1, "platform_id": "ibkr", "targets": []}),
        encoding="utf-8",
    )
    assert module.main(["--path", str(bad)]) == 1
