# IBKR website runtime daily projection

`scripts/project_ibkr_runtime_daily.py` provides an offline, pure projection of
at most 20 already-read `runtime_report.v1` objects for one trusted account target
and one New York business date. It reuses the account-facts report deployment
and single-account identity checks without requiring balance values. The caller
must supply the trusted target and ensure each report was read from the correct
protected source. Run dates use the original start time, including runs that
finish on the next day.

The projection validates execution receipts against the report and time window;
missing or invalid receipts stay unknown. Missing or contradictory report
identity, invalid times and duplicate run identities are retained as exclusions,
unmatched records or conflicts. The output contains opaque run hashes, no raw
report paths, account identifiers, order details or balances. An absent error
list is not treated as an empty error list.

All outputs remain `incomplete` with `coverage_unconfirmed`: this function does
not enumerate the archive or establish that the supplied reports cover the day.
It does not establish schedule or market-closure facts, infer that no run occurred,
or publish to the website. A future producer must implement bounded protected
source authorization and a receiver that validates a separately authorized
caller and its server-side account binding. Existing account-facts and
execution-evidence tokens do not grant IBKR daily write access.

`project_ibkr_runtime_daily_from_archive` reuses the existing protected archive
listing and report reader for the business date and following UTC date, with at
most 20 report reads; exceeding the cap is reported as truncated. The caller
supplies the trusted account configuration and runs this only in an approved
protected environment. Listing, limit and read failures remain explicit and do
not expose report paths or contents. The existing `gcloud storage ls` handles
listing internally; page-by-page completeness is not verified. The existing
reader checks the 8 MiB report size only after the subprocess has read it into
memory, not before transfer. Filename dates are only a bounded search window:
late reports, listing races and day coverage are not established. Output always
remains incomplete and no publishing or broker request occurs.

`scripts/publish_ibkr_runtime_daily.py` is a separate single-attempt publisher.
It requires the existing protected account-facts target configuration, an
explicit business date, account key, target key, console origin and dedicated
IBKR daily token in its environment. It derives the source-binding header from
the same trusted source configuration, never prints these values, and validates
the receiver's account, target, date and observation in the acknowledgement.
An error or an unverifiable acknowledgement is `unconfirmed`; no retry is made.
The script does not provision credentials, register a target or schedule itself.
