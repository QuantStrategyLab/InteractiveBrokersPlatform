# Research Notes

This directory keeps IBKR-side research artifacts that are still useful for
current live strategy review.

Live strategy behavior is owned by `UsEquityStrategies`, platform runtime
configuration, and the snapshot pipeline repositories. Historical IBKR-local
research files that used retired TQQQ, SOXL/SOXX, growth-pullback,
stock-alpha, or brokerless signal-notifier assumptions have been removed from
this repository so they are not reused as current IBKR performance references.

Current retained IBKR-local research: none.

Brokerless signal research for COIN/CONL/CONI and MAGS7 grouped tool choice now
lives in `../PaperSignalPlatform/research/`, beside the paper-signal notifier
runtime that consumes it.

Use `UsEquitySnapshotPipelines` outputs when reviewing snapshot-backed live
strategy performance.
