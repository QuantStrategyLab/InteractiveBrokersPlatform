English | [简体中文](notify-route-identity.zh-CN.md)

# Serving Telegram Read-Only Identity Diagnostic

Entry chain: `notify-route-check.yml` -> `notify-route-check-protected.yml` -> `scripts/notify_route_check.py`. Accepts manual `workflow_dispatch` on `main` only; never modifies production settings, secrets, IAM, Cloud Run, notification routes, trading, or budgets.

## Read Scope and Proof Boundaries

- Reads existing service config; private lists, target JSON, region, and GCP settings enter as reusable caller Secrets masked before runner header printing. Permissions remain `contents: read` and `id-token: write` via existing WIF/SA without creating secrets or printing `add-mask` commands or raw values.
- Describes active containers and env per revision from Cloud Run **status positive traffic** (never substituting `latestReady` or service templates). Traffic records must be valid and sum to 100% with explicit `revisionName`; split or multi-revision traffic reads each revision while keeping overall and per-row bot comparisons `unknown`/`null`.
- Only a single container's `TELEGRAM_TOKEN` is resolved. Explicit plain env tokens run `getMe` in memory only; duplicate, missing, or invalid references are never guessed, and `TG_TOKEN` is never used as a fallback.
- `secretKeyRef` versions must be exact positive integers resolved via the bound project/secret/version (supporting full resources or explicit `run.googleapis.com/secrets` aliases without cross-falling back on errors or duplicates). **`latest` or non-numeric version aliases cannot prove the immutable token loaded by running processes and stay `unknown`.**
- Comparisons check against `getMe` on the project's `<telegram-contract-secret>` `latest` version in the same run. Both `getMe` GET calls verify `ok=true`, `is_bot=true`, and positive integer `id`; missing data, read errors, or bad HTTP responses return `null`/`unknown` instead of `false`, emitting `match`/`true` or `mismatch`/`false` only when both identities are known and sole-serving traffic is unambiguous.
- All `gcloud` calls capture stdout/stderr with a 30-second timeout; `getMe` uses a 15-second timeout and 64 KiB cap. No updates, `sendMessage`, broker calls, or trading occur, and service/secret/token strings remain in private memory.

This is a point-in-time snapshot of Telegram **configuration binding**: a diagnostic source SHA cannot prove runtime images, installed code, effective channels, or live delivery, and services undergoing traffic shifts require re-observation once stable.

## Public Output

Stdout and GitHub summaries emit `qsl.notify_route_identity.v1` with fixed status/reason enums, nullable booleans, `service#N` / `revision#N` ordinals, and a validated 40-char lowercase hex diagnostic `source_sha`—never tokens, chat IDs, hashes, last4 digits, project/service/revision/SA/secret names or versions, bot IDs/usernames, raw errors, or URLs, and no private artifacts.

`status=ok` and exit `0` confirm only that the diagnostic produced output, **not that a route is ready**. Inspect per-service `bot_status=match|mismatch|unknown`, `same_bot_as_contract=true|false|null`, and fixed reasons. Scope stays `project_contract_only` with `canonical_route_status=unknown`.

`traffic_issue` / `traffic_detail` report fixed structural reasons following [Cloud Run REST v1 TrafficTarget](https://cloud.google.com/run/docs/reference/rest/v1/TrafficTarget) ("defaults to zero if unspecified"): only dictionary entries missing the `percent` key entirely fall back to zero (`percent_default_zero`), whereas explicit `null`, boolean, string, float, or out-of-range values reject; default zero never proves positive traffic, approved identity, or readiness.

## Items Outside This Diagnostic

`same_chat_as_canonical` and `same_topic_as_canonical` remain `null` (`unknown`) because no central private canonical reference exists yet and this diagnostic never creates secrets, routing rules, or topic values or reads chat secrets. Runtime chat (`QSL_GLOBAL` before `GLOBAL`), heartbeat/guard (`GLOBAL` list iteration), and central daily digest (`QSL_GLOBAL`/`GLOBAL`/plugin first ID) keep their distinct selectors under protected comparison boundaries.

This check does not inspect GHA heartbeat/guard injection, scanner/plugin/VPS installation, or central Environments, and senders inspected here do not pass `message_thread_id`. `same_bot` never implies same chat/topic, shared deduplication, or exactly-once delivery. Central production reader/`sections_v2` adoption, approved producer/runtime/source identities, safe live cycle receipts, and final Telegram verification remain separate acceptance checks.

## Local Verification

```bash
uv sync --frozen --extra test
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync python -m pytest -q tests/test_notify_route_check.py
uv run --no-sync ruff check --exclude external .
PATH=/path/to/shellcheck:$PATH actionlint
```

Regression tests use fake `gcloud`/`urllib` and synthetic routes; also run the full suite and lock/pin/startup checks listed in `CONTRIBUTING.md`. Controlled read-only diagnostics run against `main` after code checks pass, reporting missing facts honestly without coercing unknowns into matches.
