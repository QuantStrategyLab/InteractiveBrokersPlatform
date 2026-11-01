# Daily Flex collection

The `application.flex_daily` command runs separately from the trading service.
It imports the existing read-only Flex HTTP adapter and has no Gateway, order,
strategy or funding entry point. The same minimal image supports `pull` and
`watch` commands. Dependencies reuse the repository's requests and Cloud Storage
packages; the container includes neither trading dependencies nor source data.

## Coverage and checks

Coverage lives in `state/coverage.json`, separately from immutable content-addressed
`raw/` XML and `derived/` projections. Startup queues the last seven calendar days,
missed monthly controls, and oldest incomplete dates, with a maximum of 30 windows
and 25 minutes per execution. Dates use the previous complete New York calendar
day, including weekends. One prior-day opening snapshot is queued before the
configured backfill start. Missing dates are never inferred to be zero. Monthly
controls due on days 3 and 10 remain queued after an outage. Monthly activity is
compared as a multiset of source rows when every daily report is available,
with exact cash, NAV, interest and closing-position controls. Pending daily
coverage keeps the monthly control pending. These checks do not certify full
accounting reconciliation.

Monthly generation-time changes preserve completed daily coverage. Raw revisions
remain immutable evidence; only differences in semantic monthly controls queue
the corresponding daily reports again.

The collector serializes requests with at least ten seconds between them. Pending
responses poll the same reference at bounded intervals. Authentication, query and
IP errors persist a block; operators must resolve the cause before explicitly
clearing that block. An IP restriction stops collection. Do not remove an IP
allowlist or add static egress as part of this deployment. Other consumers of the
same token must share its approved request budget; this collector's lease cannot
coordinate unrelated applications.

A generation-conditioned lease excludes overlapping executions. Its 31-minute
expiry exceeds the collector's 30-minute Cloud Run termination deadline. Raw and
derived objects are committed before the date state; retries reuse identical
content and preserve old evidence. No bucket listing is required.

`complete` describes the versioned structural checks only. Missing fields, absent
accounts, wrong periods, unsupported models, duplicate identities and unqualified
corrections remain incomplete. Exact Decimal cash/NAV and observed quantity
residuals remain in private derived objects. Grant activity, independent opening
balances and settlement/accrual lineage remain explicit manual evidence needs;
no full `matched` conclusion is manufactured. XML tag and date formats must be
qualified against the dedicated query before real-data acceptance.

## Private configuration

`state/config.json` is readable only by the collector and approved operators:

```json
{
  "schema": 1,
  "query_id": "123456",
  "accounts": {"U000001": "account-1", "U000002": "account-2"},
  "backfill_start": "2026-01-01",
  "ip_restriction": "none",
  "token_expires_at": "2027-01-01T00:00:00+00:00",
  "configured_sections": ["OpenPositions", "Trades", "Transfers", "TradeTransfers", "UnsettledTransfers", "CorporateActions", "CashReport", "CashTransactions", "StmtFunds", "InterestAccruals", "OpenDividendAccruals", "ChangeInDividendAccruals", "EquitySummaryInBase", "ConversionRates", "ChangeInNAV"]
}
```

These values are synthetic. Supply actual account bindings only through approved
private configuration. Confirm configured sections from the query itself; a list
in this file does not prove the query exports them. Unknown IP restriction status
blocks broker access. Do not put credentials in this object.

`status/notification-config.json` contains only the approved private destination:
`{"chat_id":"<private-chat-id>","chat_type":"private"}`. The watchdog can read
`status/` and modify `notifications/`, but cannot read raw reports, derived amounts,
collector state, account bindings or the Flex secret. Only sanitized counts, dates,
flags and generic account labels enter the status projection.

## Deployment

Build from the repository root with `containers/flex-daily/Dockerfile`. Push only
the reviewed image and record its immutable digest. `scripts/prepare_flex_cloud.py`
renders resource specifications and IAM additions without making API calls. Supply
project, region, bucket, resource prefix, explicit operator account, image digest,
bot secret name and numeric secret versions. Generated plans contain private
resource bindings and must stay outside Git.

Use an explicit `--account=<approved-operator>` on every gcloud operation. Capture
resource IAM before and after each change. Check project/folder/organization
inheritance before creating or populating the private bucket: bucket conditions
do not restrict access inherited from broader grants. Do not overwrite an existing
bucket policy with the rendered additions or silently alter unrelated grants.

The bucket uses uniform access, enforced public access prevention, versioning,
seven-day soft deletion and default encryption. Only noncurrent state/status/
notification versions expire after 90 days; raw evidence has no deletion policy.
The collector gets read/create on raw/derived and objectUser on state/status;
the watchdog gets status reads and notification objectUser. Conditions scope
object names, not bucket listings. The scheduler gets invoker only on the two jobs,
without execution overrides, secret access or Storage access.

Create the empty Flex secret container first. The owner adds its value directly
through Secret Manager; operators verify version metadata without accessing the
payload. Mount a fixed numeric version at `/secrets/flex/token`, and mount the
approved bot's fixed version only at `/secrets/telegram/token` in the watchdog.
Deploying a mount for a version that does not exist is not a successful rollout.

Both jobs use one task, one CPU, 512 MiB and zero task retries. Timeouts are 30
minutes for pull and five minutes for watch. Scheduler uses OAuth POST to the
respective Run v2 `:run` API with `{}` body, UTC `17 10 * * *` and `47 * * * *`;
delivery has at most three retries and at least 60 seconds backoff.

## Run status and exit codes

`pull` prints one JSON line with codes and counts only: `status`,
`last_provider_error_code` (the last four-digit IBKR code seen in this run, or
null), `windows_planned`, `reports_fetched`, `windows_succeeded` and
`windows_failed`. The same counts are stored under `last_run` in
`status/latest.json`. They never include dates, accounts, amounts or error text.

- `finished` (exit 0): nothing was due, or at least one report was fetched.
- `incomplete` (exit 3): windows were due but no report was fetched. Cloud Run
  records the execution as failed, so a run that stored nothing no longer looks
  like a success.
- `blocked` (exit 2): a persisted or newly detected block. Resolve it before
  clearing the block.

IBKR codes 1001 (not ready) and 1018 (too many requests) fail only the current
window; the collector continues with the next window. After a 1018 every later
request in the run waits at least 60 seconds. Three consecutive 1001/1018 refusals
end the run so that a throttled token is not hammered. The failed windows keep
their normal retry spacing.

## Notifications and acceptance

A watchdog execution reports missed completion after the daily schedule plus two
hours, old gaps, increased gap count, field issues, observed residuals and token
expiry thresholds at 30/7/1 days. Messages contain no amounts, securities, account
IDs, source URLs or attachments. Missing, invalid or timezone-free expiry values
produce `token_expiry_unqualified` without suppressing other alerts. Claims are
stored before sending. API success
requires `ok=true` and a message ID; unknown/in-flight outcomes are not immediately
resent, rate limits honor retry_after, and 401/403 persist a notification block.
A Bot API receipt is separate from the owner's confirmation of receipt.

A manual or scheduled `pull` is accepted only when its status line is `finished`,
at least one report was fetched, and the bucket holds objects under both `raw/` and
`derived/`. An exit code of zero, `state/` and `status/` objects, or a successful Run
API call alone do not qualify. `scripts/check_flex_daily_acceptance.py` applies
this rule to the status line and a list of object names; it never reads object
contents.

Run offline regression tests, lint, repository checks and a credential-free image
smoke test. Then qualify a bounded real pull, missing-day recovery, private IAM and
one natural Scheduler execution. Empty secret containers, local tests and successful
Run API calls do not establish real collection or full financial reconciliation.

Rollback first pauses the two schedulers, then cancels only their executions.
Remove exactly the recorded IAM additions and new jobs/identities as appropriate.
Preserve private evidence and secret versions; deletion requires separate approval.
A suggested monthly budget of USD 10 should produce alerts; an alerts-only budget does
not impose a hard spending cap. No billing change is performed by the renderer.
