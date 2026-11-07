# Runtime target baseline deployment preflight

The protected Cloud Run deployment workflow checks every selected target before
image build/push/deploy, environment sync, traffic reconciliation, or scheduler
sync. Both image-only and environment-only dispatches run the check.

The check reads the final serialized `RUNTIME_TARGET_JSON` from `SYNC_PLAN_JSON`.
Image deployment also reads the existing Cloud Run service template, because
the image rollout inherits that environment before environment sync runs.
Every checked target must have a `live_continuity.baseline_target_sha256` matching
the pinned QuantPlatformKit `runtime_target_fingerprint`. Missing continuity,
missing/invalid digests, malformed targets, and failed service reads block the
workflow. Disabled and paper targets receive the same baseline check.

The planner resolves protected inventory or legacy runtime-target inputs,
applies an optional recovery ledger, normalizes service/profile fields, and
serializes the result into `targets[].env.RUNTIME_TARGET_JSON`. Environment sync
writes that serialized value to Cloud Run. Baseline records remain external
deployment inputs; the preflight neither creates nor updates them.

The fingerprint excludes `live_continuity` according to the shared contract, so
continuity state transitions retain their existing meaning. All other target
fields, including strategy revision, risk binding, and scheduler configuration,
are covered. A deliberate target change requires a separately authorized matching
baseline. Existing runtime compatibility behavior and deployment/traffic approval
flags are unchanged. Fixed notification overlays preserve the runtime target and
keep their separate existing digest checks.

Run against an already prepared private plan without printing it:

```sh
PYTHONPATH=. uv run --no-sync python scripts/verify_runtime_target_baseline.py
# For an image rollout, also check the inherited service target:
PYTHONPATH=. uv run --no-sync python scripts/verify_runtime_target_baseline.py \
  --check-deployed --project="$GCP_PROJECT_ID" --region="$CLOUD_RUN_REGION"
```

The command reads `SYNC_PLAN_JSON` from the environment. It emits only a count or
a fixed failure message, and performs no cloud writes or baseline rebasing. This
preflight proves target/baseline consistency at inspection time; it does not
attest baseline authorization, prevent concurrent external configuration writes,
or replace deployment readback and business acceptance.
