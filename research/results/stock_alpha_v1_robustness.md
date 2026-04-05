# qqq_plus_stock_alpha_v1 robustness

## Base candidate
- universe=leadership_liquid
- holdings=16
- single_cap=8%
- sector_cap=30%
- hold_bonus=0.10
- regime=qqq_breadth
- breadth thresholds: soft=55%, hard=35%
- normalization=sector
- residual proxy=simple excess return vs QQQ

## Parameter stability (5 bps, full sample top 12)
| scenario | holdings_count | single_name_cap | sector_cap | hold_bonus | CAGR | Sharpe | information_ratio_vs_qqq | rolling_36m_alpha_positive_ratio | 2022 Return | 2023+ CAGR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| grid_h12_cap9_sector25_hold05 | 12 | 0.090000 | 0.250000 | 0.050000 | 0.231133 | 0.879712 | 0.240686 | 0.912055 | -0.096328 | 0.420151 |
| grid_h12_cap9_sector30_hold05 | 12 | 0.090000 | 0.300000 | 0.050000 | 0.231133 | 0.879712 | 0.240686 | 0.912055 | -0.096328 | 0.420151 |
| grid_h12_cap9_sector35_hold05 | 12 | 0.090000 | 0.350000 | 0.050000 | 0.223027 | 0.849178 | 0.214476 | 0.904473 | -0.096366 | 0.403227 |
| grid_h12_cap8_sector25_hold05 | 12 | 0.080000 | 0.250000 | 0.050000 | 0.225313 | 0.885556 | 0.213101 | 0.912813 | -0.092314 | 0.408362 |
| grid_h12_cap8_sector30_hold05 | 12 | 0.080000 | 0.300000 | 0.050000 | 0.225313 | 0.885556 | 0.213101 | 0.912813 | -0.092314 | 0.408362 |
| grid_h12_cap9_sector25_hold15 | 12 | 0.090000 | 0.250000 | 0.150000 | 0.219733 | 0.845877 | 0.199587 | 0.919636 | -0.091287 | 0.373107 |
| grid_h12_cap9_sector30_hold15 | 12 | 0.090000 | 0.300000 | 0.150000 | 0.219733 | 0.845877 | 0.199587 | 0.919636 | -0.091287 | 0.373107 |
| grid_h12_cap9_sector35_hold15 | 12 | 0.090000 | 0.350000 | 0.150000 | 0.216442 | 0.832911 | 0.189710 | 0.924185 | -0.096639 | 0.359210 |
| grid_h12_cap8_sector35_hold05 | 12 | 0.080000 | 0.350000 | 0.050000 | 0.217617 | 0.855055 | 0.187493 | 0.907506 | -0.092350 | 0.392304 |
| grid_h12_cap9_sector25_hold10 | 12 | 0.090000 | 0.250000 | 0.100000 | 0.215540 | 0.833190 | 0.184706 | 0.909022 | -0.097443 | 0.378277 |
| grid_h12_cap9_sector30_hold10 | 12 | 0.090000 | 0.300000 | 0.100000 | 0.215540 | 0.833190 | 0.184706 | 0.909022 | -0.097443 | 0.378277 |
| grid_h12_cap8_sector25_hold15 | 12 | 0.080000 | 0.250000 | 0.150000 | 0.214541 | 0.852176 | 0.172965 | 0.920394 | -0.087248 | 0.363662 |

## Stability axis summary
| axis | axis_value | mean_cagr | median_cagr | std_cagr | mean_ir | mean_2022 | mean_2023_plus |
| --- | --- | --- | --- | --- | --- | --- | --- |
| holdings_count | 12.000000 | 0.211347 | 0.211349 | 0.011305 | 0.154069 | -0.088942 | 0.368528 |
| holdings_count | 16.000000 | 0.200964 | 0.201400 | 0.004467 | 0.119973 | -0.125363 | 0.331118 |
| holdings_count | 20.000000 | 0.199301 | 0.198632 | 0.001418 | 0.102900 | -0.126419 | 0.319386 |
| single_name_cap | 0.070000 | 0.199601 | 0.200049 | 0.004240 | 0.102139 | -0.110457 | 0.331691 |
| single_name_cap | 0.080000 | 0.205126 | 0.201400 | 0.008544 | 0.132926 | -0.114467 | 0.341994 |
| single_name_cap | 0.090000 | 0.206884 | 0.201400 | 0.010831 | 0.141877 | -0.115800 | 0.345347 |
| sector_cap | 0.250000 | 0.204805 | 0.200255 | 0.009158 | 0.128873 | -0.113362 | 0.341346 |
| sector_cap | 0.300000 | 0.204805 | 0.200255 | 0.009158 | 0.128873 | -0.113362 | 0.341346 |
| sector_cap | 0.350000 | 0.202002 | 0.200651 | 0.008070 | 0.119197 | -0.114000 | 0.336340 |
| hold_bonus | 0.050000 | 0.207908 | 0.206516 | 0.010362 | 0.140891 | -0.116739 | 0.351729 |
| hold_bonus | 0.100000 | 0.201080 | 0.198579 | 0.006396 | 0.115052 | -0.113908 | 0.336471 |
| hold_bonus | 0.150000 | 0.202624 | 0.200255 | 0.007961 | 0.120999 | -0.110077 | 0.330833 |

### Stability platform stats
- best_full_sample_CAGR=0.2311
- best_full_sample_IR_vs_QQQ=0.2407
- plateau_within_100bps_and_positive_IR=5 (6.2%)
- plateau_within_200bps_and_positive_IR=15 (18.5%)

## Regime robustness (5 bps, full sample)
- spy_breadth: benchmark=SPY, breadth=eligible universe 中 sma200_gap > 0 的比例
- qqq_breadth: benchmark=QQQ, breadth=eligible universe 中 sma200_gap > 0 的比例
- qqq_xlk_smh_breadth: benchmark=QQQ, breadth=(XLK 与 SMH 在 200 日线上方的 ETF 比例)
- ETF data assumption: QQQ / XLK / SMH 均来自 yfinance/Yahoo，2018-01-01 之后可直接取到
| scenario | CAGR | Sharpe | information_ratio_vs_qqq | rolling_36m_alpha_positive_ratio | 2022 Return | 2023+ CAGR |
| --- | --- | --- | --- | --- | --- | --- |
| regime_qqq_breadth | 0.201400 | 0.822800 | 0.121524 | 0.874147 | -0.126033 | 0.337210 |
| regime_spy_breadth | 0.189854 | 0.811835 | 0.061063 | 0.812737 | -0.160760 | 0.323089 |
| regime_qqq_xlk_smh_breadth | 0.170272 | 0.745469 | -0.014595 | 0.808946 | -0.156651 | 0.315424 |

## Data pressure tests (5 bps, full sample)
| scenario | data_variant | universe_lag_rebalances | universe_filter | group_normalization | CAGR | Sharpe | information_ratio_vs_qqq | rolling_36m_alpha_positive_ratio | 2022 Return | 2023+ CAGR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| normalization_universe | alias_on | 0 | leadership_liquid | universe | 0.239188 | 0.883998 | 0.278379 | 0.914329 | -0.021460 | 0.411894 |
| leadership_ultra_liquid_100m | alias_on | 0 | leadership_ultra_liquid_100m | sector | 0.218815 | 0.869163 | 0.193584 | 0.918878 | -0.073221 | 0.361832 |
| alias_off_no_identifier_repair | alias_off | 0 | leadership_liquid | sector | 0.217570 | 0.877685 | 0.182167 | 0.931766 | -0.125164 | 0.339161 |
| alias_on_baseline | alias_on | 0 | leadership_liquid | sector | 0.201400 | 0.822800 | 0.121524 | 0.874147 | -0.126033 | 0.337210 |
| universe_lag_1_rebalance | alias_on | 1 | leadership_liquid | sector | 0.192962 | 0.801909 | 0.086002 | 0.900682 | -0.134672 | 0.321744 |

## Cost / turnover profile
| cost_bps_one_way | CAGR | Total Return | Max Drawdown | Sharpe | annual_turnover | average_monthly_turnover | average_names_replaced_per_rebalance | median_holding_duration_days | top5_continuity |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.000000 | 0.203547 | 3.608203 | -0.372877 | 0.829505 | 3.571252 | 0.294500 | 4.755102 | 61.000000 | 0.588235 |
| 5.000000 | 0.201400 | 3.540848 | -0.373027 | 0.822800 | 3.571252 | 0.294500 | 4.755102 | 61.000000 | 0.588235 |
| 10.000000 | 0.199256 | 3.474463 | -0.373177 | 0.816091 | 3.571252 | 0.294500 | 4.755102 | 61.000000 | 0.588235 |

## Walk-forward / OOS
| scenario | period | CAGR | Total Return | Max Drawdown | Sharpe | information_ratio_vs_qqq | rolling_36m_alpha_positive_ratio |
| --- | --- | --- | --- | --- | --- | --- | --- |
| offensive_default_candidate | OOS 2022-2026 | 0.209749 | 1.243526 | -0.299533 | 0.859083 | 0.470904 | 1.000000 |
| offensive_default_candidate | OOS 2022 | -0.127418 | -0.126033 | -0.172500 | -0.746351 | 0.898298 | 1.000000 |
| offensive_default_candidate | OOS 2023+ | 0.337210 | 1.567061 | -0.299533 | 1.162719 | 0.313599 | 1.000000 |
| offensive_is_selected | OOS 2022-2026 | 0.234521 | 1.445060 | -0.348407 | 0.889424 | 0.554331 | 1.000000 |
| offensive_is_selected | OOS 2022 | -0.097719 | -0.096639 | -0.160390 | -0.510590 | 1.032882 | 1.000000 |
| offensive_is_selected | OOS 2023+ | 0.359210 | 1.706626 | -0.348407 | 1.152046 | 0.390975 | 1.000000 |
| defensive_baseline | OOS 2022-2026 | 0.184469 | 1.051214 | -0.254393 | 0.885560 | 0.344425 | 1.000000 |
| defensive_baseline | OOS 2022 | -0.141182 | -0.139659 | -0.171444 | -1.225741 | 0.723841 | 1.000000 |
| defensive_baseline | OOS 2023+ | 0.307094 | 1.384188 | -0.254393 | 1.234418 | 0.169338 | 1.000000 |
| QQQ | OOS 2022-2026 | 0.101948 | 0.509796 | -0.348280 | 0.533389 |  | 0.000000 |
| QQQ | OOS 2022 | -0.328892 | -0.325770 | -0.348280 | -1.071928 |  | 0.000000 |
| QQQ | OOS 2023+ | 0.282076 | 1.239289 | -0.227683 | 1.355202 |  | 0.000000 |

## Relative attribution vs QQQ
| strategy | beta_vs_qqq | alpha_ann_vs_qqq | tracking_error_vs_qqq | information_ratio_vs_qqq | up_capture_vs_qqq | down_capture_vs_qqq | active_share_vs_qqq |
| --- | --- | --- | --- | --- | --- | --- | --- |
| defensive_baseline | 0.465097 | 0.081736 | 0.210717 | -0.104102 | 0.595210 | 0.293529 |  |
| qqq_plus_stock_alpha_v1 | 0.722446 | 0.079869 | 0.214569 | 0.121524 | 0.783036 | 0.446906 |  |

## Shadow tracking gate
- recommendation=no_shadow_tracking
- reason=fails at least one promotion gate
- oos_positive_rolling_alpha_ratio=100.0%
- oos_cagr_minus_qqq_5bps=0.1078
- oos_max_drawdown=-0.2995
- annual_turnover=3.5713
- plateau_200bps_share=18.5%

### Gate thresholds used
- OOS rolling 36m alpha > 0 的窗口占比 >= 60%
- 5 bps 成本后 OOS CAGR 不低于 QQQ
- OOS MaxDD <= 40%
- annual turnover <= 5.0x
- 参数局部网格里，至少 20% 组合落在 best-200bps 且 IR>0 的平台内

## Active share vs QQQ note
- 这里无法准确给出真正的 active share vs QQQ，因为当前公开数据链路没有 QQQ 历史 constituent weights。输出里显式记为 NaN。