# qqq_plus_stock_alpha_v1.1 spec lock

## 结论先看
- previous_default=qqq_plus_stock_alpha_v1
- default_frozen_spec=v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10
- aggressive_alt_spec=v11_leadership_liquid_norm_universe_cross_sectional_h12_cap9_sector35_hold10
- 原 promotion gate（上一轮）=no_shadow_tracking
- 原 promotion gate（V1.1 局部收敛空间）=no_shadow_tracking
- V1.1 recommendation layer=not_ready
- reason=edge exists but spec-lock / platform width is still insufficient

## 选型规则（robustness_score）
- 0.30 * pct_rank(OOS CAGR - QQQ)
- 0.20 * pct_rank(OOS rolling 36m alpha > 0 ratio)
- 0.15 * plateau_200bps_share（同 universe+normalization+holdings 子空间）
- 0.10 * pct_rank(OOS MaxDD，越浅越好)
- 0.10 * pct_rank(2022 return)
- 0.07 * pct_rank(2023+ CAGR)
- 0.05 * pct_rank(annual turnover，越低越好)
- 0.03 * complexity_score（相对上一轮默认规格的改动越少越高）

## Top candidates by robustness_score
| scenario | robustness_score | robustness_rank | full_cagr_rank | universe_filter | group_normalization_label | holdings_count | single_name_cap | sector_cap | hold_bonus | oos_cagr_minus_qqq | plateau_200bps_share | oos_max_drawdown | annual_turnover | return_2022 | cagr_2023_plus |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10 | 0.806944 | 1 | 48 | liquid_50m | universe_cross_sectional | 12 | 0.080000 | 0.300000 | 0.100000 | 0.232062 | 0.333333 | -0.323779 | 3.440286 | -0.015312 | 0.464792 |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap8_sector35_hold05 | 0.803102 | 2 | 12 | leadership_liquid | universe_cross_sectional | 12 | 0.080000 | 0.350000 | 0.050000 | 0.237184 | 0.962963 | -0.330845 | 3.551850 | -0.011143 | 0.470237 |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap8_sector35_hold10 | 0.803102 | 2 | 7 | leadership_liquid | universe_cross_sectional | 12 | 0.080000 | 0.350000 | 0.100000 | 0.228163 | 0.962963 | -0.330845 | 3.388142 | -0.020395 | 0.461524 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector25_hold10 | 0.797917 | 4 | 44 | liquid_50m | universe_cross_sectional | 12 | 0.080000 | 0.250000 | 0.100000 | 0.231043 | 0.333333 | -0.323779 | 3.428160 | -0.017529 | 0.464345 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap9_sector30_hold10 | 0.767731 | 5 | 40 | liquid_50m | universe_cross_sectional | 12 | 0.090000 | 0.300000 | 0.100000 | 0.238979 | 0.333333 | -0.333093 | 3.585400 | -0.019058 | 0.476468 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap10_sector30_hold10 | 0.767731 | 5 | 40 | liquid_50m | universe_cross_sectional | 12 | 0.100000 | 0.300000 | 0.100000 | 0.238979 | 0.333333 | -0.333093 | 3.585400 | -0.019058 | 0.476468 |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap8_sector35_hold15 | 0.766343 | 7 | 19 | leadership_liquid | universe_cross_sectional | 12 | 0.080000 | 0.350000 | 0.150000 | 0.217948 | 0.962963 | -0.332161 | 3.277791 | -0.025839 | 0.449348 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap9_sector25_hold10 | 0.755139 | 8 | 34 | liquid_50m | universe_cross_sectional | 12 | 0.090000 | 0.250000 | 0.100000 | 0.237955 | 0.333333 | -0.333093 | 3.573274 | -0.021266 | 0.476018 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap10_sector25_hold10 | 0.755139 | 8 | 34 | liquid_50m | universe_cross_sectional | 12 | 0.100000 | 0.250000 | 0.100000 | 0.237955 | 0.333333 | -0.333093 | 3.573274 | -0.021266 | 0.476018 |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap9_sector35_hold05 | 0.751343 | 10 | 3 | leadership_liquid | universe_cross_sectional | 12 | 0.090000 | 0.350000 | 0.050000 | 0.245595 | 0.962963 | -0.340458 | 3.702623 | -0.015054 | 0.484140 |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap10_sector35_hold05 | 0.751343 | 10 | 3 | leadership_liquid | universe_cross_sectional | 12 | 0.100000 | 0.350000 | 0.050000 | 0.245595 | 0.962963 | -0.340458 | 3.702623 | -0.015054 | 0.484140 |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap9_sector35_hold10 | 0.742824 | 12 | 1 | leadership_liquid | universe_cross_sectional | 12 | 0.090000 | 0.350000 | 0.100000 | 0.236278 | 0.962963 | -0.340458 | 3.532852 | -0.024270 | 0.474999 |

## Plateau 复核
| neighborhood | count | total | share |
| --- | --- | --- | --- |
| top_decile_full_cagr | 23 | 216 | 0.106481 |
| within_100bps_best_cagr_ir_positive | 18 | 216 | 0.083333 |
| within_200bps_best_cagr_ir_positive | 35 | 216 | 0.162037 |

### Plateau by key axis (within 200bps)
| dimension | value | share | count | total |
| --- | --- | --- | --- | --- |
| group_normalization | universe | 0.324074 | 35 | 108 |
| group_normalization | sector | 0.000000 | 0 | 108 |
| hold_bonus | 0.050000 | 0.166667 | 12 | 72 |
| hold_bonus | 0.100000 | 0.166667 | 12 | 72 |
| hold_bonus | 0.150000 | 0.152778 | 11 | 72 |
| holdings_count | 12 | 0.324074 | 35 | 108 |
| holdings_count | 16 | 0.000000 | 0 | 108 |
| universe_filter | leadership_liquid | 0.240741 | 26 | 108 |
| universe_filter | liquid_50m | 0.083333 | 9 | 108 |

## spy_breadth sanity check（仅前 3 名）
| scenario | spy_sanity_full_cagr | spy_sanity_full_ir_vs_qqq | spy_sanity_oos_cagr | spy_sanity_oos_cagr_minus_qqq | spy_sanity_pass |
| --- | --- | --- | --- | --- | --- |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10 | 0.229672 | 0.200518 | 0.307194 | 0.205246 | True |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap8_sector35_hold05 | 0.254424 | 0.317501 | 0.323003 | 0.221054 | True |
| v11_leadership_liquid_norm_universe_cross_sectional_h12_cap8_sector35_hold10 | 0.251707 | 0.309412 | 0.315515 | 0.213567 | True |

## Full / OOS / 2022 / 2023+（5 bps）
| strategy | period | CAGR | Total Return | Max Drawdown | Sharpe | Information Ratio vs QQQ | rolling_36m_alpha_positive_ratio | annual_turnover |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| previous_offensive_default | Full Sample | 0.201400 | 3.540848 | -0.373027 | 0.822800 | 0.121524 | 0.874147 | 3.571252 |
| previous_offensive_default | OOS Sample | 0.209749 | 1.243526 | -0.299533 | 0.859083 | 0.470904 | 1.000000 | 3.571252 |
| previous_offensive_default | 2022 | -0.127418 | -0.126033 | -0.172500 | -0.746351 | 0.898298 |  | 3.571252 |
| previous_offensive_default | 2023+ | 0.337210 | 1.567061 | -0.299533 | 1.162719 | 0.313599 | 1.000000 | 3.571252 |
| default_frozen_spec | Full Sample | 0.241651 | 4.958780 | -0.323779 | 0.903840 | 0.259556 | 0.952995 | 3.440286 |
| default_frozen_spec | OOS Sample | 0.334010 | 2.397303 | -0.323779 | 1.117224 | 0.836495 | 1.000000 | 3.440286 |
| default_frozen_spec | 2022 | -0.015491 | -0.015312 | -0.163557 | -0.019576 | 1.170391 |  | 3.440286 |
| default_frozen_spec | 2023+ | 0.464792 | 2.450132 | -0.323779 | 1.325631 | 0.714971 | 1.000000 | 3.440286 |
| aggressive_alt_spec | Full Sample | 0.268277 | 6.098166 | -0.408751 | 0.914116 | 0.379497 | 0.922669 | 3.532852 |
| aggressive_alt_spec | OOS Sample | 0.338226 | 2.443099 | -0.340458 | 1.085907 | 0.868592 | 1.000000 | 3.532852 |
| aggressive_alt_spec | 2022 | -0.024552 | -0.024270 | -0.159130 | -0.000751 | 1.265346 |  | 3.532852 |
| aggressive_alt_spec | 2023+ | 0.474999 | 2.528740 | -0.340458 | 1.321844 | 0.733802 | 1.000000 | 3.532852 |
| defensive_baseline | Full Sample | 0.163311 | 2.481396 | -0.276198 | 0.854919 | -0.104102 | 0.914329 | 3.926458 |
| defensive_baseline | OOS Sample | 0.184469 | 1.051214 | -0.254393 | 0.885560 | 0.344425 | 1.000000 | 3.926458 |
| defensive_baseline | 2022 | -0.141182 | -0.139659 | -0.171444 | -1.225741 | 0.723840 |  | 3.926458 |
| defensive_baseline | 2023+ | 0.307094 | 1.384188 | -0.254393 | 1.234418 | 0.169338 | 1.000000 | 3.926458 |
| QQQ | Full Sample | 0.179222 | 2.894099 | -0.351187 | 0.811457 |  | 0.012130 | 0.000000 |
| QQQ | OOS Sample | 0.101948 | 0.509796 | -0.348280 | 0.533390 |  | 0.000000 | 0.000000 |
| QQQ | 2022 | -0.328891 | -0.325770 | -0.348280 | -1.071929 |  |  | 0.000000 |
| QQQ | 2023+ | 0.282076 | 1.239289 | -0.227683 | 1.355201 |  | 0.066667 | 0.000000 |

## Relative-to-QQQ attribution
| strategy | beta_vs_qqq | alpha_ann_vs_qqq | tracking_error_vs_qqq | information_ratio_vs_qqq | up_capture_vs_qqq | down_capture_vs_qqq | turnover | average_names_held | active_share_vs_qqq |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| defensive_baseline | 0.465097 | 0.081736 | 0.210717 | -0.104102 | 0.595210 | 0.293529 | 3.926458 | 20.644166 |  |
| previous_offensive_default | 0.722445 | 0.079869 | 0.214569 | 0.121524 | 0.783036 | 0.446906 | 3.571252 | 13.762777 |  |
| default_frozen_spec | 0.680133 | 0.126004 | 0.246611 | 0.259556 | 0.803347 | 0.303760 | 3.440286 | 9.465767 |  |
| aggressive_alt_spec | 0.828586 | 0.127691 | 0.248930 | 0.379497 | 0.936444 | 0.410912 | 3.532852 | 10.322083 |  |

## Plateau key stats
- old_round_plateau_200bps_share=18.5%
- v1_1_local_grid_plateau_200bps_share=16.2%
- within_100bps_share=8.3%
- top_decile_share=10.6%

## Active share note
- active_share_vs_QQQ 继续显式记为 NaN，因为当前公开数据链路没有 QQQ 历史 constituent weights。
- 如果后续拿到 QQQ 历史 constituent weights，可在同一回测日期轴上对比组合权重与 QQQ 成分权重，补做真正 active share。