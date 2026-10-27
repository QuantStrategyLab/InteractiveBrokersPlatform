"""Exercise the actual cloud reader with synthetic private objects and no network."""
import json
from subprocess import CompletedProcess

import pytest

from scripts import summarize_execution_ledger_cloud as diagnostic


@pytest.mark.parametrize("value, timestamp, expected", [
    ("2026-10-23", False, "2026-10-23"), ("2026-02-30", False, None),
    ("private-value", False, None), (None, False, None),
    ("2026-10-23T23:30:00-02:00", True, "2026-10-24"),
    ("2026-10-23T23:30:00", True, None), ("private-value", True, None),
])
def test_dates_are_validated_and_timestamps_use_utc(value, timestamp, expected):
    assert diagnostic._claim_date(value, timestamp=timestamp) == expected


def test_unresolved_dates_are_aggregate_scoped_and_do_not_imply_broker_orders(monkeypatch):
    prefixes = {name: f"gs://private/{name}/v1/ibkr/private-account/private-profile/live/"
                for name in ("execution_markers", "execution_outcomes")}
    monkeypatch.setattr(diagnostic, "_scoped_prefixes", lambda **_: prefixes)
    root = "v1/ibkr/private-account/private-profile/live/"
    records = {}
    for suffix, schema, claimed in [
        ("2026-10-20/2026-10-21/no-contract", "execution_claim.v1", "2026-10-21T00:00:00Z"),
        ("2026-10-22/2026-10-23/no-contract", "execution_claim.v1", "2026-10-23T00:00:00Z"),
        ("no-signal-date/no-effective-date/no-contract", "execution_claim.v1", None),
        ("2026-10-19/2026-10-20/no-contract", "execution_marker.v1", None),
    ]:
        records[prefixes["execution_markers"] + suffix + ".json"] = {
            "schema_version": schema, "marker_key": root + suffix, "claimed_at": claimed,
            "metadata": {"private-order": "must-not-appear"},
        }
    resolved = "2026-10-22/2026-10-23/no-contract"
    records[prefixes["execution_outcomes"] + resolved + ".json"] = {
        "schema_version": "execution_outcome.v1", "marker_key": root + resolved,
    }
    def cloud(*args):
        if args[:2] == ("storage", "ls"):
            prefix = args[3][:-2]
            return CompletedProcess(args, 0, "\n".join(k for k in records if k.startswith(prefix)).encode(), b"")
        assert args[:2] == ("storage", "cat")
        return CompletedProcess(args, 0, json.dumps(records[args[2]]).encode(), b"")
    monkeypatch.setattr(diagnostic, "_gcloud", cloud)
    result = diagnostic.summarize(service="private-service", project="private-project", region="region")
    assert result["bounded_complete"] is True
    assert result["claim_count"] == 3 and result["completion_marker_count"] == 1
    assert result["outcome_count"] == 1 and result["claim_without_outcome_count"] == 2
    assert result["claim_without_outcome_date_counts"] == [
        {"signal_date": None, "effective_date": None, "claimed_date_utc": None, "count": 1},
        {"signal_date": "2026-10-20", "effective_date": "2026-10-21", "claimed_date_utc": "2026-10-21", "count": 1},
    ]
    public = json.dumps(result)
    assert not any(x in public for x in ("private-", "must-not-appear", "marker_key", "broker_orders"))
    # Identity mismatch still fails; date projection never relaxes the protected scope.
    next(iter(records.values()))["marker_key"] = root.replace("private-account", "another-account") + "2026-10-20/2026-10-21/no-contract"
    with pytest.raises(ValueError, match="identity mismatch"):
        diagnostic.summarize(service="private-service", project="private-project", region="region")
