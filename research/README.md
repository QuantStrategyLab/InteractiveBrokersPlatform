# Research Notes

This directory keeps IBKR-side research artifacts that are still useful for
current live strategy review.

Live strategy behavior is owned by `UsEquityStrategies`, platform runtime
configuration, and the snapshot pipeline repositories. Historical IBKR-local
research files that used retired TQQQ, SOXL/SOXX, growth-pullback, or
stock-alpha assumptions have been removed so they are not reused as current
performance references.

Current retained research:

- `backtest_video_qqq_tqqq_dual_drive.py`
- `results/video_qqq_tqqq_dual_drive_comparison.csv`
- `results/video_qqq_tqqq_dual_drive_recommendation.json`
- `results/video_qqq_tqqq_dual_drive_summary.md`
- `backtest_video_qqq_tqqq_position_scaling.py`
- `results/video_qqq_tqqq_position_scaling_comparison.csv`
- `results/video_qqq_tqqq_position_scaling_recommendation.json`
- `results/video_qqq_tqqq_position_scaling_summary.md`
- `backtest_video_qqq_tqqq_growth_optimizations.py`
- `results/video_qqq_tqqq_growth_optimizations_comparison.csv`
- `results/video_qqq_tqqq_growth_optimizations_recommendation.json`
- `results/video_qqq_tqqq_growth_optimizations_summary.md`

Use `UsEquitySnapshotPipelines` outputs when reviewing snapshot-backed live
strategy performance.
