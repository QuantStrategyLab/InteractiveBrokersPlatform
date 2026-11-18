"""Thin adapter: map IBKR claim facts onto QPK execution_kernel T1.

N13: consult-only. Local claim gate still wins; T1 return value must not change
control flow (including when dedup is off). T2/T3 are intentionally not wired —
IBKR has no submission_halted / outcome_unknown isomorphism with Schwab #505.
"""

from __future__ import annotations

from quant_platform_kit.execution_kernel import (
    ExecutionGuardDecision,
    ExecutionMode,
    LiveSubmitSnapshot,
    deny_live_submit_without_durable_identity,
)


def consult_t1_live_submit(
    *,
    identity_held: bool,
    dry_run_bypass: bool = False,
    mode: ExecutionMode = ExecutionMode.LIVE,
) -> ExecutionGuardDecision:
    """Consult T1 for a live broker-submit attempt.

    Callers on the claim-fail path pass ``identity_held=False`` and still raise
    the existing RuntimeError. Callers after a successful claim may pass
    ``identity_held=True`` as a shadow consult; deny must not block submit.
    """
    return deny_live_submit_without_durable_identity(
        LiveSubmitSnapshot(
            mode=mode,
            dry_run_bypass=bool(dry_run_bypass),
            identity_held=bool(identity_held),
        )
    )
