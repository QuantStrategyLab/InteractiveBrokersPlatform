# Fixed notification marker check

The existing Deploy Cloud Run entry adds two mutually exclusive manual modes.
`binding-read` reads only the original business-binding fields and never
requests cloud authentication. `execute-probe` requires its separate explicit
approval and the original project, deployer and runtime identity. Both require
main, attempt 1, the exact controller commit in `source_commit`, configured
target, and all image/env/traffic/Scheduler/old-notification controls false.
Unknown modes cannot select a deployment job. Existing callers without the new
mode retain their original behavior.
All nonempty, non-disabled modes receive the fixed isolated run title. The
existing lifecycle follow-up admits only the ordinary deployment title, so an
isolated, failed, skipped or unknown-mode run cannot start its inventory jobs.
Direct and scheduled lifecycle runs keep their existing behavior.

The binding read accepts only the approved TQQQ target fingerprint and its
original profile/mode. It selects one complete matching binding, or globals
whose complete target matches. Missing fields may use globals only with that
same complete target; a shared strategy or alias is insufficient. Credential
fields stop extraction. The four-field row is written only to a new runner
private file (0600); only its hash, tuple hash, slot hash and booleans are
uploaded. No selectors, raw identities, aliases, assets or private row are
logged or uploaded. It does not write the expected list. A missing binding is a
safe stop, not permission to invent an identity.

The probe creates one nonce-named Cloud Run Job in interactivebrokersquant,
us-central1, using the existing c995 immutable image and original runtime SA.
Its Python command overrides the application entrypoint, uses isolated stdlib
only, clears its own environment and contacts only metadata email/token and
one fixed synthetic GCS object. It creates with generation 0, checks correct
and rejected CAS operations, and retains the final synthetic object. Earlier
generation numbers are observations, not promises that bucket versioning
retains earlier payloads. No object or Job deletion, bucket policy/lifecycle,
IAM, production service, strategy, order, Scheduler or notification operation
is present. Short-lived metadata credentials stay in memory.

The original WIF control client first requires structured 404/NOT_FOUND for
the exact Job, creates once, verifies its image/command/resources/identity and
runs once with its read-back etag. Stale etag or any denial/unknown stops; no
retry, alternate identity or reuse of an existing Job. It reads only the exact
execution's anonymous log certificate. The Job and synthetic object stay for
separately authorized cleanup. A failed/unknown write may leave an object;
reported generations and the write-outcome flag preserve that uncertainty.
Both text and structured JSON certificates are supported. A bound completed
failed execution remains failed, but its nonce-bound diagnostic whitelist is
retained too; unknown fields and arbitrary error messages are never exported.
HTTP denial, transport failure or an unbound operation stops before log reads.

Limits: one task, parallelism 1, retries 0, 1 CPU/512Mi, task timeout 60 seconds;
probe HTTP deadline 45 seconds and at most 24 requests. Control requests have
a 180-second deadline. These bound resource use, not the cloud bill. There is
no new build/image/subscription. The 0.05 USD probe reserve is an estimate,
not a billing hard cap; account free allowance, actual startup/log/retention
charges remain unknown. The existing cumulative 1 USD decision limit remains.
Source integration/CI is not permission to execute the probe. Before any cloud
execution, record the exact merged controller commit, retained-object scope,
cost estimate and explicit approval; this source work performs no cloud probe.
