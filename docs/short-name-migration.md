# Protected short-name destination migration

The existing Deploy workflow admits only inventory services that already exist.
`short-name-migration.yml` supplies a separate destination observation and inert
initialization channel for the four references `S07-C01` through `S07-C04`.
It never updates an existing service, inventory, traffic allocation, Scheduler,
IAM policy, Secret value or trading switch. Source naming remains governed by
[the naming contract](naming-trading-diff.zh-CN.md).

## Protection and inputs

Run only from `main`, attempt 1, through the existing protected Environment named
by `IBKR_DEPLOY_ENVIRONMENT`, using its existing WIF credentials. Workflow
permissions are `contents: read` and job-scoped `id-token: write`. The cloud
identity needs project observation, destination service observation and operation
observation; validation and initialization additionally need service creation and
`iam.serviceAccounts.actAs` for the approved disabled identity. This change grants
none of those permissions. A missing binding stops the run. Use a disabled
identity without broker, account, Secret or trading authority; its exact identity
belongs only in protected configuration.

The Environment secret `IBKR_SHORT_NAME_MIGRATION_PLAN_JSON` is the complete
approved plan, not a free-form dispatch payload. Its object has exactly:

- `mapping_ref`: `authority.values.s07-ibkr-rename-draft-r1-mapping`.
- `source_revision`: the exact source revision approved for this migration.
- `project`, `region`, `disabled_service_account`: protected infrastructure bindings.
- `targets`: exactly four ordered objects, each with `reference`, `old`, `new`,
  `image` and `expected_uid`. Names must be distinct across both sets; each new
  name must fit the existing service-plus-tag limit. `image` is an immutable
  regional Artifact Registry digest in the approved project. `expected_uid` is
  empty for an absent service, or the exact previously observed service UID.

The script pins the source revision from the merged naming change. Operators must
resolve the mapping in the restricted authority store and compare all four pairs
before preparing the Environment plan. A matching reference string alone does not
prove the names were resolved correctly. Dispatch must repeat the exact mapping
reference and source revision and supply `plan_sha256`, computed over UTF-8 JSON
with sorted keys and compact separators. No binding or raw resource name belongs
in dispatch inputs, Git, public logs or artifacts.

Environment configuration is a separate owner operation. This Draft does not
create or populate any secret, identity or Environment. A future deployment card
must identify the accepted workflow revision and exact plan; the historical
four-target deployment scope is retained, not consumed by this implementation.

## Modes and disabled behavior

`observe` performs read-only project and destination GETs. A service 404 means
absent only after successful active-project readback. Authentication, transport,
malformed JSON, redirects and other HTTP failures block. Existing destinations require an
explicit matching UID and the exact inert configuration, ready revision and
traffic readback; an unknown existing service is a collision. No prior inventory
admission is required for new names.

`validate` requires a separate current authorization and sends service creation
requests with the API's `validateOnly=true` for absent destinations. It creates
nothing. `initialize` first completes those validations for all four, then
re-reads each destination immediately before an atomic create. It never patches
or replaces an existing service. An existing accepted destination is checked
again and left unchanged. A concurrent creator, collision or failure stops the
run; no automatic retry, deletion or rollback follows.

A first Cloud Run service cannot use an existing service's no-traffic deployment
baseline. Initialization therefore creates an **inert disabled service**, with
internal-only ingress, IAM invocation checks, zero minimum instances and a fixed
Python HTTP process that returns a fixed disabled response with status 503. It does not launch the application entry point, mount a
volume, inject an account/Secret/environment variable or connect to IB Gateway.
Its initial revision receives 100% of that new service's traffic; that revision
runs only the inert process. Old services and their traffic remain untouched.
This is disabled-target initialization, not a no-traffic application release or
cutover. Later application deployment and inventory adoption require a separate
approved path; this entry cannot perform them.

Every validation/create needs the Environment secret
`IBKR_SHORT_NAME_MIGRATION_AUTHORIZATION_JSON` with `action` equal to
`initialize-disabled`, an `authority_ref` for the separate owner card, and exact
`run_id`, `workflow_sha`, `plan_sha256`, `source_revision`, `mapping_ref` and
`expires_at` (timezone-aware ISO timestamp, at most one hour ahead). The Draft card
and historical deployment approval alone are rejected as run authority. Approval
is checked before authentication and again before each POST, after obtaining its
access token. Environment protection, restricted secret maintenance and the
single workflow concurrency group remain required operational protections.

## Readback, restart and evidence

Creation waits on the returned operation, validates its service UID, then GETs
the service again. The ready/created revision must agree; observed generation
must match current generation; reconciliation must be finished; traffic must be
exactly 100% to that inert revision with no tag. The service's identity, image
digest, inert command, empty environment, service account, ingress and scaling
must match the protected plan. Proto3 JSON omission of default false/zero fields
is interpreted using the documented API defaults; required identities and readiness
fields still need explicit evidence. These values stay in runner memory. The uploaded
result contains only four reference states and revision/traffic verification
booleans; it does not expose revisions, UIDs, names, image paths or private hashes.

Polling reads may repeat, but mutations never retry. Timeout, process loss or
ambiguous POST outcome leaves `BLOCKED_OR_OUTCOME_UNKNOWN`; partial creation is
not rolled back. Observe and reconcile cloud state before a separately authorized
continuation; record exact UIDs privately and prepare a fresh plan/run card.
Workflow reruns are blocked. No automatic action is authorized by an artifact or
an observation result. The source revision binding and approved digest are intent
constraints, not an attestation of image build provenance or production adoption.
Live, PAPER, Kelly and every stop-loss switch stay outside this channel.
