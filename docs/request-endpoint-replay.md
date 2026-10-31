# Synthetic request endpoint replay

## Purpose

The request-report extraction is checked at two boundaries. The native reporting
component observations in PR #915 cover the extracted helper. The endpoint replay
adds the surrounding Flask route, early returns, exception handling, decision
finalizer, and report-persistence boundary. The historical-source validator
recognizes the exact legacy and reviewed refactored report identities. Runtime
request behavior and execution authority are unchanged.

The replay uses synthetic TQQQ and SOXL inputs. It is not a replay of recorded
production traffic or authenticated historical account records. It cannot
establish deployment state, strategy correctness, order execution, account
continuity, or permission to trade.

## Executed boundary

`tests/test_request_endpoint_replay.py` imports the complete runtime through the
existing `strategy_module_factory` test fixture and calls the public `/run` and
`/dry-run` routes with Flask's test client. The fixture supplies synthetic
configuration and broker/cloud SDK modules. Runtime startup under real SDKs and
production configuration is outside this comparison.

The test executes native report construction, report finalization, signal
snapshots, execution receipts, decision-receipt handling, and runtime-log
serialization. A native reporting adapter receives fixed clocks and identifiers.
Strategy execution, market observation, lock ownership, report storage, and
notification delivery use explicit in-memory boundaries. No broker connection,
cloud object-store operation, notification, subprocess, or output-file operation
is permitted during each observation. Attempted forbidden operations fail the
case even if application code catches the immediate exception.
The guard restores the prior cached `main` module, or removes the replay import
when none existed, after both successful and exceptional observations. Identity
checks ensure denied persistence aliases cannot leak into later tests.

Thirty cases cover both profiles across ordinary submission, pending
reconciliation, an expected risk guard, strategy and gateway failures, permission
failure, overlapping requests, market closure, disabled targets, dry-run POST and
GET, rejected methods, failed and unconfirmed persistence, and conflicting
receipts. Additional controls omit finalizers or persistence and change response
status; the comparison must detect each drift. Normalized identity changes and
promotion of an unknown outcome to a fill must remain validator failures.

## Observation contract

Each request records its response status, body and headers, complete report
objects, and ordered callbacks. Report, persistence, log and notification payloads
are copied when each callback occurs. Later mutations therefore cannot overwrite
an earlier observation. A persistence attempt and its simulated return are
separate events; neither represents a real stored object.

The canonical comparison sorts JSON object keys and HTTP header names. `Allow`
tokens are sorted because their order is not significant. It preserves every
other field, missing versus null values, JSON value types, and event-array order.
Clock and identifier inputs are fixed before execution; emitted fields are not
removed to make the outputs match.

The compact fixture at `tests/fixtures/request-endpoint-replay.json` contains
canonical observation hashes and expected source-validator result hashes. Its
provenance identifies the frozen legacy producer and all nine reviewed producer
file digests. The complete synthetic observation captures remain separate review
evidence. A future intentional contract change must review and regenerate the
affected expectations rather than accepting a mismatched digest.

## Source comparison

The frozen legacy producer is `main.py` from
`d220b2dcc83decb0931aa0088dd3a9f9d119a383`, SHA-256
`f13b55f501c404ef249bb3344bd69136beb8e0ead382e827d082217180b46637`.
The request-report refactor retains `main.py` SHA-256
`ef3ab072868dcb3447b19d224c63879cfa11a3d3906db7d25ac51b77171feb04`
in PR #915 at `4b84fd6e556b4c95f54bfe02280579bc5803e25f`.

The offline comparison loads each exact source in a separate process against the
same frozen test and native-helper inputs. The source loader substitutes the
complete `main` module, rather than reconstructing a handler from selected AST
nodes. All other registered producer files are checked against their exact
digests. The local comparison also verifies every loaded QPK, U.S. strategy, and
Hong Kong strategy source file against its pinned repository blob. This source
overlay check is separate from installation validation in the frozen CI environment.
Repository CI runs the checked-out source against the frozen legacy
observations. A check of one source revision does not prove that another source
revision was executed; the separate pairwise replay receipts identify the
before-and-after comparison explicitly.

## Validator and acceptance boundary

Captured reports are wrapped in the existing synthetic source-intake fixture.
Its locator, account-binding and revision declarations are unauthenticated test
inputs. Complete supported observations can be locally `CONSISTENT`, while the
overall result remains `BLOCKED`. Missing account observations stay blocked;
observed contradictions stay failures.

The registry retains the legacy nine-file projection and adds the exact reviewed
refactored projection. Only the `main.py` digest differs; the other eight required
digests are identical. The complete required projection must match a reviewed
identity. Additional file metadata remains allowed, and declared revision strings
remain syntax-checked declarations rather than authenticated source provenance.
The daily-projection producer is unchanged.

An unknown or incomplete producer remains blocked. A changed helper cannot be
accepted merely because `main.py` is known. Unsupported fingerprints still report
`PRODUCER_VERSION_UNSUPPORTED` alongside independent contradictions. The endpoint
fixture retains its original legacy and unregistered expectations and adds an
explicit refactored expectation equal to the reviewed legacy projection.

Every replay result keeps `execution_authorized`, `production_ready`, and
`historical_parity_established` false. Source, identity, receipt and unknown-state
checks are unchanged. Matching a registered producer does not authenticate
historical inputs, establish deployment state or permit execution.

## Running the committed checks

```bash
PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync python -m pytest -q tests/test_request_endpoint_replay.py tests/test_request_cycle_source_equivalence.py tests/test_historical_source_evidence.py tests/test_historical_producer_registration.py
```

Use the repository's frozen test environment. The ordinary CI test suite includes
these files. These checks perform no GCS writes and do not require live credentials.
