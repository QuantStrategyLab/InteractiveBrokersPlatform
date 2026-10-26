# One-time Client Portal ledger capture

`scripts.capture_web_portfolio_ledger` collects one account's currency ledger
after an operator has established a read-only Client Portal Gateway (CPGW)
session. It is an operator tool, with no runtime registration or automatic run.
It does not authenticate, refresh a login, initialize a brokerage session,
publish account facts, or authorize spending.

## Host and session requirements

Use an approved, temporary protected Linux host. Keep the login browser, CPGW,
and collector on that host: IBKR requires both authentication and API requests
on the same machine. A browser on the operator's laptop plus an API tunnel alone
does not satisfy that documented arrangement. See the official
[installation](https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/installation-authentication)
and [limitations](https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/limitations-of-the-client-portal-gateway).

Do not install this environment on the production TWS/Gateway host or reuse its
session. Use private authenticated access to the remote browser; bind CPGW and
any desktop bridge to loopback, with no public listener for either. Download
CPGW from IBKR, record the archive digest, and run it as a dedicated unprivileged
user. Use the repository's pinned production Python dependencies. Trust the
installed CPGW certificate/CA explicitly, verify its localhost identity, and keep
TLS verification enabled. No `verify=False`, password, OTP, cookie, or bearer
token arguments are supported.

The operator enters broker credentials and completes 2FA in that remote browser.
Choose the read-only/account-management path if offered. If login requires
terminating another session or enabling trading, stop before accepting it. The
collector's GET allowlist does not prove that an earlier browser login preserved
the original session. Check the existing protected runtime's session evidence
before and after; an ambiguous result leaves that check unresolved. IBKR
[documents the two session tiers and one brokerage session per username](https://www.interactivebrokers.com/docs/web-api/authentication/sessions).
CPGW is not a permanent unattended authentication solution; see its
[daily reauthentication requirement](https://www.interactivebrokers.com/docs/web-api/authentication/cpgw/client-portal-gateway-faq).

## Private request and invocation

Resolve the expected physical account and existing execution-report prefix from
the current protected target/account-group source. Match profile and selector
before preparing the request. Keep its exact source hash in
`binding_source_sha256`; this is a caller-supplied locator hash, not an
independent issuer attestation or a runtime snapshot binding.

Write the following shape to an owner-only `0600` regular file on that Linux
host. All values below are synthetic. The collector rejects duplicate JSON keys,
unknown fields, symlink request files, unsafe identifiers, and non-GCS prefixes.

```json
{
  "schema_version": "ibkr.web_ledger_capture_request.v1",
  "account_id": "SYNTH123",
  "currency": "USD",
  "gcp_project_id": "synthetic-project",
  "report_prefix": "gs://synthetic-private/reports/tqqq",
  "capture_id": "1f72542a-599c-4d46-a288-ac3d083d80ef",
  "binding_source_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Run from the repository root **on that approved Linux host**, only after its
login page and private access have actually been verified:

```bash
python -m scripts.capture_web_portfolio_ledger \
  --request-file /run/private-ledger/request.json \
  --ca-file /run/private-ledger/cpgw-ca.pem
```

The authenticated existing QPK GCS client must already have create/get access
within the approved prefix. The tool does not grant IAM, fetch broker secrets,
or create compute resources. Supply cloud credentials through the approved
restricted client; never through command arguments or shell tracing.

## Requests, saved evidence, and failure handling

The only broker requests are, sequentially:

1. `GET https://localhost:5000/v1/api/portfolio/accounts`, with one unambiguous
   matching account. Conflicting `id`/`accountId` values stop collection.
2. `GET /v1/api/portfolio/{accountId}/ledger` for that exact account.

The [accounts prerequisite](https://www.interactivebrokers.com/docs/web-api/v1/endpoints/portfolio/portfolio-accounts)
and [ledger fields](https://www.interactivebrokers.com/docs/web-api/v1/endpoints/portfolio/portfolio-ledger)
follow IBKR's documented non-tiered account endpoints. Tiered structures are
unsupported here; do not broaden the query to other accounts to make it pass.

The transport disables environment proxies and redirects and performs no retry.
It accepts a bounded JSON response, validates explicit account/currency identity,
and preserves `cashbalance` and `settledcash` as decimal strings, including zero
and negative observations. Missing and invalid fields remain failures. No BASE
fallback, cash arithmetic, or freshness certification is performed. Source epoch
seconds and collection start/receive times are recorded separately; later use
must check source age and align the native snapshot independently.

Only the selected five ledger fields, source response hashes, binding hash and
collection metadata are saved, with `synthetic=false`. Response bodies, account
titles, unrelated accounts/currencies, headers, cookies and internal `sessionid`
are not archived. Response hashes support a later exact-content comparison;
they cannot reconstruct discarded bodies. The evidence is a minimal source
projection, not a complete raw-response archive.

The object path is `{report_prefix}/web-portfolio-ledger/v1/{capture_id}.json`.
Saving reuses `capture_pilot_release_source._save_once`: create-only generation
precondition, no upload retry, then exact byte readback. An existing differing
object or unknown write outcome stops the operation. Do not repeat collection
under a new ID to hide an unresolved write; inspect that object through the
existing protected channel first. Console output contains fixed statuses and
the saved evidence hash, never account identity, amounts, source URI or errors.

This observation is **not** the synthetic `packet.cash_evidence.web_api_ledger`
contract and must not be relabeled synthetic to enter that research path. It
does not establish complete obligations, native snapshot agreement, freshness,
original-session preservation, risk-policy approval, or adoption readiness.

## End of the one-time session

After readback, or on an unresolved login/query/save failure, stop collection.
Close the temporary browser and CPGW, stop the private tunnel/desktop bridge,
remove the temporary request and cloud credentials, and delete the temporary
host and its auto-delete disk. Do not call a global broker logout endpoint or
change the production session. Remove only newly created access bindings and
temporary resources, and verify deletion from the control plane. Retain the
protected observation and permitted status summary under their existing policy.

A prepared script is not a running login page. Resource creation, human login,
the live read, session preservation and cleanup each require their own observed
result before they can be reported complete.
