# cash_buffer_branch daily overlay research

## Baseline
- strategy=cash_buffer_branch_default
- family=tech_heavy_pullback
- monthly stock exposures={"risk_on": 0.8, "soft_defense": 0.6, "hard_defense": 0.0}
- holdings=8
- single_name_cap=10%
- sector_cap=40%
- hold_bonus=0.10
- benchmark=QQQ
- non-rebalance days=no-op baseline; overlay only changes exposure / existing-name weights / BOXX

## Overlay families tested
- daily_portfolio_throttle_overlay
- daily_name_level_trim_overlay
- daily_portfolio_plus_name_overlay (only if A/B both showed value)

## Baseline reference (5 bps, OOS)
| strategy | CAGR | Max Drawdown | Information Ratio vs QQQ | alpha_ann_vs_qqq | return_2022 | cagr_2023_plus | Turnover/Year | Average Stock Weight | Average BOXX Weight |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cash_buffer_branch_default_monthly_baseline | 0.340680 | -0.191763 | 0.874353 | 0.256477 | -0.120121 | 0.526419 | 2.798286 | 0.566385 | 0.433615 |

## Family selection table (5 bps, OOS)
| strategy | family | overlay_selection_score | OOS CAGR | OOS MaxDD | Information Ratio vs QQQ | alpha_ann_vs_qqq | 2022 Return | 2023+ CAGR | Turnover/Year | Average Stock Weight | Average BOXX Weight | Overlay Trigger Frequency | Average Days In Throttle | Average Days Trimmed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cash_buffer_branch_default_monthly_baseline | monthly_baseline | 0.800000 | 0.340680 | -0.191763 | 0.874353 | 0.256477 | -0.120121 | 0.526419 | 2.798286 | 0.566385 | 0.433615 | 0.000000 | 0.000000 | 0.000000 |
| daily_name_level_trim_overlay__20dma_half_confirm2 | daily_name_level_trim_overlay | 0.770000 | 0.254582 | -0.148093 | 0.562249 | 0.195607 | -0.112283 | 0.395667 | 8.218125 | 0.468099 | 0.531901 | 0.632864 | 0.000000 | 12.481481 |
| daily_name_level_trim_overlay__50dma_zero_confirm2 | daily_name_level_trim_overlay | 0.550000 | 0.214020 | -0.156768 | 0.381910 | 0.169430 | -0.113352 | 0.337437 | 9.655560 | 0.421972 | 0.578028 | 0.581221 | 0.000000 | 12.380000 |
| daily_portfolio_throttle_overlay__qqq_dma_mom_6state | daily_portfolio_throttle_overlay | 0.480000 | 0.206767 | -0.134282 | 0.344992 | 0.166449 | -0.113148 | 0.326901 | 12.115107 | 0.413333 | 0.586667 | 0.404695 | 8.450980 | 0.000000 |
| daily_portfolio_throttle_overlay__held_breadth_5state | daily_portfolio_throttle_overlay | 0.400000 | 0.163511 | -0.128741 | 0.166580 | 0.132740 | -0.086712 | 0.253634 | 12.624690 | 0.342723 | 0.657277 | 0.512676 | 11.142857 | 0.000000 |

## Best portfolio overlay
| strategy | family | cost_bps_one_way | period | Start | End | Total Return | CAGR | Max Drawdown | Volatility | Sharpe | Sortino | Calmar | Turnover/Year | Rebalances/Year | Average Names Held | Beta vs QQQ | Information Ratio vs QQQ | Up Capture vs QQQ | Down Capture vs QQQ | 2022 Return | 2023+ CAGR | beta_vs_qqq | alpha_ann_vs_qqq | tracking_error_vs_qqq | information_ratio_vs_qqq | up_capture_vs_qqq | down_capture_vs_qqq | rolling_36m_alpha_mean | rolling_36m_alpha_median | rolling_36m_alpha_last | rolling_36m_alpha_positive_ratio | Average Stock Weight | Average BOXX Weight | Overlay Trigger Frequency | Average Days In Throttle | Average Days Trimmed | Days In Throttle Share | Days Trimmed Share | Degraded Signal Share | overlay_trigger_frequency | average_days_in_throttle | average_days_trimmed | days_in_throttle_share | days_trimmed_share | degraded_signal_share | full_cagr | full_max_drawdown | return_2022 | cagr_2023_plus | score_oos_rel_qqq | score_oos_cagr | score_oos_ir | score_oos_alpha | score_oos_maxdd | score_2022 | score_turnover | overlay_selection_score |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| daily_portfolio_throttle_overlay__qqq_dma_mom_6state | daily_portfolio_throttle_overlay | 5.000000 | OOS Sample | 2022-01-03 | 2026-04-02 | 1.220149 | 0.206767 | -0.134282 | 0.164145 | 1.231099 | 1.459564 | 1.539789 | 12.115107 | 49.721129 | 5.448405 | 0.285801 | 0.344992 | 0.688788 | 0.159194 | -0.113148 | 0.326901 | 0.285801 | 0.166449 | 0.224382 | 0.344992 | 0.688788 | 0.159194 | 0.151454 | 0.150012 | 0.201459 | 1.000000 | 0.413333 | 0.586667 | 0.404695 | 8.450980 | 0.000000 | 0.404695 | 0.000000 | 0.000000 | 0.373854 | 8.516484 | 0.000000 | 0.373854 | 0.000000 | 0.000000 | 0.124650 | -0.337037 | -0.113148 | 0.326901 | 0.400000 | 0.400000 | 0.400000 | 0.400000 | 0.800000 | 0.600000 | 0.400000 | 0.480000 |

## Best name-level trim overlay
| strategy | family | cost_bps_one_way | period | Start | End | Total Return | CAGR | Max Drawdown | Volatility | Sharpe | Sortino | Calmar | Turnover/Year | Rebalances/Year | Average Names Held | Beta vs QQQ | Information Ratio vs QQQ | Up Capture vs QQQ | Down Capture vs QQQ | 2022 Return | 2023+ CAGR | beta_vs_qqq | alpha_ann_vs_qqq | tracking_error_vs_qqq | information_ratio_vs_qqq | up_capture_vs_qqq | down_capture_vs_qqq | rolling_36m_alpha_mean | rolling_36m_alpha_median | rolling_36m_alpha_last | rolling_36m_alpha_positive_ratio | Average Stock Weight | Average BOXX Weight | Overlay Trigger Frequency | Average Days In Throttle | Average Days Trimmed | Days In Throttle Share | Days Trimmed Share | Degraded Signal Share | overlay_trigger_frequency | average_days_in_throttle | average_days_trimmed | days_in_throttle_share | days_trimmed_share | degraded_signal_share | full_cagr | full_max_drawdown | return_2022 | cagr_2023_plus | score_oos_rel_qqq | score_oos_cagr | score_oos_ir | score_oos_alpha | score_oos_maxdd | score_2022 | score_turnover | overlay_selection_score |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| daily_name_level_trim_overlay__20dma_half_confirm2 | daily_name_level_trim_overlay | 5.000000 | OOS Sample | 2022-01-03 | 2026-04-02 | 1.618168 | 0.254582 | -0.148093 | 0.181831 | 1.342701 | 1.767918 | 1.719068 | 8.218125 | 84.360968 | 6.101313 | 0.389330 | 0.562249 | 0.816321 | 0.167005 | -0.112283 | 0.395667 | 0.389330 | 0.195607 | 0.212496 | 0.562249 | 0.816321 | 0.167005 | 0.155768 | 0.153942 | 0.217937 | 1.000000 | 0.468099 | 0.531901 | 0.632864 | 0.000000 | 12.481481 | 0.000000 | 0.632864 | 0.000000 | 0.622769 | 0.000000 | 13.309278 | 0.000000 | 0.622769 | 0.000000 | 0.172910 | -0.284488 | -0.112283 | 0.395667 | 0.800000 | 0.800000 | 0.800000 | 0.800000 | 0.600000 | 0.800000 | 0.800000 | 0.770000 |

## Best combo overlay
_skipped because A/B did not both clear the incremental-value filter_

## 5 bps comparison set
| strategy | family | CAGR | cagr_minus_qqq | Max Drawdown | return_2022 | cagr_2023_plus | Turnover/Year | Average Names Held | Average Stock Weight | Average BOXX Weight | beta_vs_qqq | alpha_ann_vs_qqq | Information Ratio vs QQQ | Up Capture vs QQQ | Down Capture vs QQQ | Overlay Trigger Frequency | Average Days In Throttle | Average Days Trimmed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| daily_name_level_trim_overlay__20dma_half_confirm2 | daily_name_level_trim_overlay | 0.254582 | 0.152634 | -0.148093 | -0.112283 | 0.395667 | 8.218125 | 6.101313 | 0.468099 | 0.531901 | 0.389330 | 0.195607 | 0.562249 | 0.816321 | 0.167005 | 0.632864 | 0.000000 | 12.481481 |
| daily_portfolio_throttle_overlay__qqq_dma_mom_6state | daily_portfolio_throttle_overlay | 0.206767 | 0.104818 | -0.134282 | -0.113148 | 0.326901 | 12.115107 | 5.448405 | 0.413333 | 0.586667 | 0.285801 | 0.166449 | 0.344992 | 0.688788 | 0.159194 | 0.404695 | 8.450980 | 0.000000 |
| cash_buffer_branch_default_monthly_baseline | monthly_baseline | 0.340680 | 0.238732 | -0.191763 | -0.120121 | 0.526419 | 2.798286 | 6.101313 | 0.566385 | 0.433615 | 0.502780 | 0.256477 | 0.874353 | 1.010057 | 0.137830 | 0.000000 | 0.000000 | 0.000000 |
| qqq_plus_current_default | reference | 0.334010 | 0.232062 | -0.323779 | -0.015312 | 0.464792 | 3.706698 | 10.570356 | 0.760188 | 0.239812 | 0.744797 | 0.241211 | 0.836495 | 1.200446 | 0.399330 | 0.000000 | 0.000000 | 0.000000 |
| russell_1000_multi_factor_defensive | reference | 0.184469 | 0.082521 | -0.254393 | -0.139659 | 0.307094 | 4.530278 | 24.000000 | 0.759287 | 0.240713 | 0.568919 | 0.122988 | 0.344425 | 0.826915 | 0.413046 | 0.000000 | 0.000000 | 0.000000 |
| QQQ | reference | 0.101948 | 0.000000 | -0.348280 | -0.325770 | 0.282076 | 0.000000 | 1.000000 | 1.000000 | 0.000000 | 1.000000 | 0.000000 |  | 1.000000 | 1.000000 | 0.000000 | 0.000000 | 0.000000 |

## Recommendation
- overlay_has_incremental_value=False
- best_overlay_family=daily_name_level_trim_overlay
- best_overlay_strategy=daily_name_level_trim_overlay__20dma_half_confirm2
- recommended_upgrade_direction=do_not_upgrade_overlay
- next_step=直接推进 paper 下单

## Explicit caveats
- This stays research-only; no runtime / Cloud Run / paper config is changed here.
- Month-start still chooses the stock list. Overlay never adds names mid-month.
- Reduced stock exposure always parks in BOXX (no hidden new sleeve).
- Daily signals can degrade to monthly baseline when short lookback indicators are unavailable; degraded-signal share is reported explicitly.