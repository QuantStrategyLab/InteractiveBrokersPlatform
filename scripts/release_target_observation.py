"""Bounded local classification of an already-read target; never emits values."""
from collections.abc import Mapping
import re

SHA = re.compile(r"[0-9a-f]{40}\Z")


def _revision_state(value, lock):
    if value is None:
        return "absent"
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        return "invalid"
    if not isinstance(lock, str) or SHA.fullmatch(lock) is None:
        return "comparison_unavailable"
    return "matches_source_lock" if value == lock else "differs_from_source_lock"


def classify(target, lock):
    """Shape and equality only; no artifact presence or promotion conclusion."""
    release = target.get("strategy_release")
    if release is None:
        shape = "absent"
    else:
        try:
            from quant_platform_kit.common.strategy_release import build_strategy_release_identity
            build_strategy_release_identity(release)
        except (ValueError, TypeError):
            shape = "invalid"
        except Exception:
            shape = "unavailable"
        else:
            shape = "valid"
    revision_state = _revision_state(release.get("strategy_revision"), lock) if isinstance(release, Mapping) else (
        "absent" if release is None else "invalid"
    )
    risk = target.get("runtime_risk_limits")
    binding = risk.get("binding") if isinstance(risk, Mapping) else None
    if risk is not None and not isinstance(risk, Mapping):
        risk_state = "invalid"
    elif binding is not None and not isinstance(binding, Mapping):
        risk_state = "invalid"
    else:
        risk_state = _revision_state(binding.get("ues_revision") if binding else None, lock)
    return {
        "release_identity": shape,
        "release_revision": revision_state,
        "risk_binding_revision": risk_state,
        "source_lock_revision": "observed" if isinstance(lock, str) and SHA.fullmatch(lock) else "unavailable",
        "artifact_qualification": "not_assessed",
    }
