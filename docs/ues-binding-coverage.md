# Build-time UES binding coverage

The existing deployment guard compares the installed UES revision in the built
image against `uv.lock` and the revisions present in planned runtime targets.
It still runs after build and before push/deploy, with the same blocking rules.
This is build-time evidence, **not proof of the live deployed revision**.

`--report-json PATH` optionally writes a schema-version-1 JSON report. The report
is an observation of the existing validation, not a new configuration source or
validation policy. Failure to write this optional report warns without changing
the guard exit code. The workflow renders the report into its job summary even
when the guard blocks deployment; a missing report is reported as unavailable.

Statuses:
- `verified`: every resolved target had at least one expected revision, and all
  present expected revisions matched the installed revision. This does not mean
  both binding fields were present or that other runtime requirements passed.
- `not_covered`: at least one target had neither revision binding. Such targets
  remain non-blocking under the existing rules. Mixed verified/unbound coverage
  is never reported as wholly verified.
- `mismatch`: a present expected revision mismatched, or installed revision
  sources disagreed. Deployment remains blocked.
- `uncomputable`: no runtime target could be resolved. Deployment remains blocked.
  Target counts also use this state when installed sources disagree, so target
  comparisons could not be performed.
- `error`: revision extraction or input parsing failed, including the existing
  300-second image-probe timeout. Deployment remains blocked.

Reports have fixed fields: schema version, evidence scope, status, aggregate
counts by state, and installed revisions from the three allowlisted sources
(`image`, `lockfile`, `explicit`). Only full hexadecimal commit SHAs are retained;
other revision strings become null. Reports contain no target names, release IDs,
accounts, assets, raw runtime configuration, environment values or probe stderr.
Console validation uses ordinal target aliases and similarly sanitized values.

`--summarize-report PATH` only renders allowlisted enum/count values from an
existing report. It does **not** execute validation, Docker, or deployment and its
exit code must not be used as a validation result. The deploy step still uses the
normal guard exit code; report generation and rendering add no enforcement.

All tests use synthetic targets and mocked Docker subprocess results. No live
broker, Cloud Run, credentials or deployment workflows are exercised.
