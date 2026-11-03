# Artifact Registry policy maintenance

The manual `Artifact Registry maintenance` workflow repairs the six-policy
contract required by the deployment KEEP guard. It runs from `main` only and
uses the `ibkr-live` Environment. That Environment must require the owner's
approval, disallow administrator bypass and allow only the `main` branch.
Allow self-review so the designated owner can approve their own manual dispatch;
this does not bypass the required approval.

## Protected configuration

Mirror the existing deployment WIF provider, project and deploy identity into
Environment Secrets named `GCP_WORKLOAD_IDENTITY_PROVIDER`,
`IBKR_GCP_PROJECT_ID` and `IBKR_DEPLOY_SERVICE_ACCOUNT`. Mirror its deployment
region into `CLOUD_RUN_REGION`. Set `IBKR_AR_CANDIDATE_DIGEST` to the separately
approved immutable candidate digest. No key file is used. The first step rejects
missing configuration and registers derived identity masks before authentication.
The workflow never prints cloud responses, tokens, resource identities or digests.

The existing service account must already have repository get/update permissions
(`roles/artifactregistry.repoAdmin` on the selected repository supplies these).
The permission probe runs before inspection or repair. Missing/unknown permission
stops the run; the workflow never grants IAM permissions.

## Policy behavior

`config/artifact-registry-cleanup-policies.json` preserves the six expected names.
The blanket `sha256:` KEEP protects every SHA-256 image, including current,
pending, rollback and future candidates. KEEP takes precedence over DELETE.
The other KEEP rules retain ten and two versions per package. DELETE rules retain
the reviewed 30-day tagged, seven-day untagged and 14-day untagged age bounds.
This repair produces no effective SHA-256 image deletion or storage savings.
Removing the blanket KEEP requires separate authorization and review.

The helper requires a fresh Cloud Run service, revision, Job and active execution
inventory and an explicit repository cleanup-mode boolean. It refuses unknown
identity or state. Repairs preserve cleanup mode and compare a fresh repository
snapshot immediately before writing; exact policy and mode readback follows the
write. Failed or ambiguous writes are never retried automatically. Inspection and
repair dry-run validate the proposal and inventory without writing policies or
changing repository cleanup mode. Dry-run here is local validation, not a cloud
cleanup simulation.

GitHub concurrency serializes maintenance attempts across queued runs and runner
restarts. Every new invocation reads current cloud state; no local success marker
can authorize a write. Other workflows and external operators are not covered by
this lock. The snapshot comparison detects observed drift, but is not a server
compare-and-set. Coordinate other policy writers during maintenance; blanket KEEP
protects images appearing concurrently with a deployment.

## Approved execution sequence

After the reviewed PR is merged, dispatch `mode=inspect`, then `mode=repair` with
`dry_run=true`, then `mode=repair` with `dry_run=false`. Each dispatch needs a fresh
Environment approval. Stop on any failure; do not automatically retry. Compare the
count-only before/after evidence and cleanup-mode result. Deployment is separate:
only the existing approved single redeploy may follow successful maintenance.
Read back its revision, readiness, full traffic allocation, image digest and
in-image strategy revision under the deployment's own approval scope.

This workflow does not deploy, shift traffic, touch Scheduler or IB Gateway,
change trading/risk switches, change IAM or delete images. Tests use synthetic
identities and cloud command doubles; they do not prove production permissions,
serving state or deployment success.
