# qqq_plus_stock_alpha_v1.1b candidate-centric gate review

## Frozen center spec
- universe=liquid_50m
- normalization=universe_cross_sectional
- regime=qqq_breadth
- holdings=12
- single_cap=8%
- sector_cap=30%
- hold_bonus=0.10
- exposures=100/60/0
- residual proxy=simple excess return vs QQQ
- cost assumption(main)=5 bps one-way

## One-step neighborhood (5 bps, OOS)
| strategy | change_dimension | change_direction | from_value | to_value | CAGR | alpha_ann_vs_qqq | Max Drawdown | delta_oos_cagr_vs_center | delta_oos_alpha_ann_vs_center | delta_oos_maxdd_vs_center |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10__holdings_count_up_16 | holdings_count | up | 12.000000 | 16.000000 | 0.268687 | 0.189853 | -0.340024 | -0.065324 | -0.051359 | -0.016246 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10__single_name_cap_up_0p09 | single_name_cap | up | 0.080000 | 0.090000 | 0.340928 | 0.246972 | -0.333093 | 0.006917 | 0.005761 | -0.009315 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10__sector_cap_down_0p25 | sector_cap | down | 0.300000 | 0.250000 | 0.332991 | 0.240419 | -0.323779 | -0.001019 | -0.000792 | -0.000000 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10__sector_cap_up_0p35 | sector_cap | up | 0.300000 | 0.350000 | 0.295239 | 0.211368 | -0.334324 | -0.038771 | -0.029844 | -0.010545 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10__hold_bonus_down_0p05 | hold_bonus | down | 0.100000 | 0.050000 | 0.311237 | 0.224124 | -0.336957 | -0.022774 | -0.017087 | -0.013178 |
| v11_liquid_50m_norm_universe_cross_sectional_h12_cap8_sector30_hold10__hold_bonus_up_0p15 | hold_bonus | up | 0.100000 | 0.150000 | 0.292072 | 0.209326 | -0.323779 | -0.041938 | -0.031885 | -0.000000 |

## Candidate-centric plateau
- original_global_plateau_200bps_share=16.2%
- neighbor_count=6
- neighbor_plateau_50bps_share=16.7%
- neighbor_plateau_100bps_share=33.3%
- neighbor_plateau_200bps_share=33.3%

## Monthly jackknife (5 bps)
- positive alpha share after removing any single month=100.0%
### Worst 5 removed months (lowest post-removal alpha)
| removed_label | CAGR | alpha_ann_vs_qqq | Max Drawdown | delta_oos_alpha_ann_vs_center |
| --- | --- | --- | --- | --- |
| 2024-11 | 0.270147 | 0.204243 | -0.323779 | -0.036969 |
| 2024-02 | 0.274519 | 0.208470 | -0.323779 | -0.032741 |
| 2025-01 | 0.289559 | 0.215208 | -0.331159 | -0.026004 |
| 2025-07 | 0.290790 | 0.216350 | -0.323779 | -0.024861 |
| 2024-03 | 0.295855 | 0.218182 | -0.323779 | -0.023029 |

### Best 5 removed months (highest post-removal alpha)
| removed_label | CAGR | alpha_ann_vs_qqq | Max Drawdown | delta_oos_alpha_ann_vs_center |
| --- | --- | --- | --- | --- |
| 2024-12 | 0.371014 | 0.275188 | -0.323779 | 0.033976 |
| 2025-11 | 0.373176 | 0.272837 | -0.323779 | 0.031625 |
| 2022-07 | 0.334010 | 0.265562 | -0.323779 | 0.024351 |
| 2023-04 | 0.356750 | 0.263277 | -0.323779 | 0.022066 |
| 2024-06 | 0.340287 | 0.261320 | -0.323779 | 0.020109 |

## Continuous block holdout (5 bps)
- 6m block positive alpha share=100.0%
- 12m block positive alpha share=100.0%
### Worst 5 removed 6m blocks
| removed_label | CAGR | alpha_ann_vs_qqq | delta_oos_alpha_ann_vs_center |
| --- | --- | --- | --- |
| 2024-08->2025-01 | 0.193498 | 0.167719 | -0.073493 |
| 2022-01->2022-06 | 0.382681 | 0.169901 | -0.071311 |
| 2022-04->2022-09 | 0.340681 | 0.178418 | -0.062793 |
| 2024-09->2025-02 | 0.214814 | 0.179067 | -0.062145 |
| 2023-12->2024-05 | 0.205994 | 0.187906 | -0.053306 |

### Best 5 removed 6m blocks
| removed_label | CAGR | alpha_ann_vs_qqq | delta_oos_alpha_ann_vs_center |
| --- | --- | --- | --- |
| 2023-01->2023-06 | 0.325561 | 0.329656 | 0.088445 |
| 2022-12->2023-05 | 0.355573 | 0.322632 | 0.081420 |
| 2022-11->2023-04 | 0.348281 | 0.309747 | 0.068536 |
| 2023-02->2023-07 | 0.315623 | 0.308169 | 0.066958 |
| 2023-04->2023-09 | 0.348890 | 0.307755 | 0.066544 |

### Worst 5 removed 12m blocks
| removed_label | CAGR | alpha_ann_vs_qqq | delta_oos_alpha_ann_vs_center |
| --- | --- | --- | --- |
| 2024-02->2025-01 | 0.099471 | 0.109728 | -0.131483 |
| 2023-12->2024-11 | 0.095852 | 0.115161 | -0.126050 |
| 2022-01->2022-12 | 0.464792 | 0.121421 | -0.119790 |
| 2024-01->2024-12 | 0.136485 | 0.152921 | -0.088290 |
| 2024-09->2025-08 | 0.144083 | 0.154966 | -0.086245 |

### Best 5 removed 12m blocks
| removed_label | CAGR | alpha_ann_vs_qqq | delta_oos_alpha_ann_vs_center |
| --- | --- | --- | --- |
| 2023-01->2023-12 | 0.313415 | 0.392483 | 0.151271 |
| 2022-10->2023-09 | 0.340470 | 0.384827 | 0.143616 |
| 2022-12->2023-11 | 0.340003 | 0.384597 | 0.143386 |
| 2022-11->2023-10 | 0.350726 | 0.381826 | 0.140615 |
| 2023-02->2024-01 | 0.316134 | 0.376089 | 0.134878 |

## Main comparison (5 bps)
| strategy | period | CAGR | Total Return | Max Drawdown | Sharpe | alpha_ann_vs_qqq | annual_turnover |
| --- | --- | --- | --- | --- | --- | --- | --- |
| default_frozen_spec | Full Sample | 0.241651 | 4.958780 | -0.323779 | 0.903840 | 0.126004 | 3.440286 |
| default_frozen_spec | OOS Sample | 0.334010 | 2.397303 | -0.323779 | 1.117224 | 0.241211 | 3.440286 |
| default_frozen_spec | 2022 | -0.015491 | -0.015312 | -0.163557 | -0.019576 | 0.068668 | 3.440286 |
| default_frozen_spec | 2023+ | 0.464792 | 2.450132 | -0.323779 | 1.325631 | 0.121421 | 3.440286 |
| aggressive_alt_spec | Full Sample | 0.268277 | 6.098166 | -0.408751 | 0.914116 | 0.127691 | 3.532852 |
| aggressive_alt_spec | OOS Sample | 0.338226 | 2.443099 | -0.340458 | 1.085907 | 0.238178 | 3.532852 |
| aggressive_alt_spec | 2022 | -0.024552 | -0.024270 | -0.159130 | -0.000751 | 0.129999 | 3.532852 |
| aggressive_alt_spec | 2023+ | 0.474999 | 2.528740 | -0.340458 | 1.321844 | 0.124098 | 3.532852 |
| defensive_baseline | Full Sample | 0.163311 | 2.481396 | -0.276198 | 0.854919 | 0.081735 | 3.926458 |
| defensive_baseline | OOS Sample | 0.184469 | 1.051214 | -0.254393 | 0.885560 | 0.122988 | 3.926458 |
| defensive_baseline | 2022 | -0.141182 | -0.139659 | -0.171444 | -1.225741 | -0.076385 | 3.926458 |
| defensive_baseline | 2023+ | 0.307094 | 1.384188 | -0.254393 | 1.234418 | 0.064774 | 3.926458 |
| QQQ | Full Sample | 0.179222 | 2.894100 | -0.351187 | 0.811457 | 0.000000 | 0.000000 |
| QQQ | OOS Sample | 0.101948 | 0.509796 | -0.348280 | 0.533389 | -0.000000 | 0.000000 |
| QQQ | 2022 | -0.328892 | -0.325770 | -0.348280 | -1.071931 | 0.000000 | 0.000000 |
| QQQ | 2023+ | 0.282076 | 1.239290 | -0.227683 | 1.355202 | 0.000000 | 0.000000 |

## Recommendation layers
- original_global_gate=no_shadow_tracking
- candidate_centric_recommendation=not_ready
- reason=center spec still fails at least one local-neighborhood or holdout stability check

## Interpretation
- 这里只复核 default_frozen_spec 自己附近是否稳，不再重新做更大网格搜参。
- active share vs QQQ 仍显式留空，因为没有 QQQ 历史 constituent weights。