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
archive reading and explicit source authorization; a receiver must validate a
separately authorized caller and match its server-side account binding. Existing
account-facts and execution-evidence tokens do not grant IBKR daily write access.
