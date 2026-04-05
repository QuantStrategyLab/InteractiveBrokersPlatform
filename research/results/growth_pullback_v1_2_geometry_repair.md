# growth_pullback_systematic_v1.2 geometry repair

## Current center spec
- name=tech_heavy_pullback_balanced_focused_qqq_breadth
- family=tech_heavy_pullback
- universe=tech_heavy
- normalization=universe_cross_sectional
- holdings=12
- single_cap=10%
- sector_cap=40%
- hold_bonus=0.05
- min_adv20=50M

## Current stable neighbor
- name=tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08
- single_cap=8%
- sector_cap=40%
- hold_bonus=0.05

## Geometry repair summary (5 bps)
| strategy | full_cagr | oos_cagr | oos_cagr_minus_qqq | full_max_drawdown | oos_max_drawdown | return_2022 | cagr_2023_plus | annual_turnover | avg_names_held | risk_on_fill_rate | geometry_repair_score |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| local_stable_neighbor | 0.200474 | 0.284838 | 0.182889 | -0.268360 | -0.163777 | -0.096223 | 0.431919 | 2.364666 | 6.505051 | 0.640000 | 0.690000 |
| explicit_cash_buffer_control | 0.192073 | 0.249317 | 0.147369 | -0.316997 | -0.177354 | -0.127418 | 0.395387 | 2.710271 | 8.848485 | 1.000000 | 0.660000 |
| center_current | 0.230343 | 0.315572 | 0.213623 | -0.327569 | -0.184113 | -0.114205 | 0.486063 | 2.958865 | 6.505051 | 0.800000 | 0.600000 |
| feasible_two_sector_50cap | 0.216009 | 0.262836 | 0.160888 | -0.394470 | -0.212659 | -0.164401 | 0.434188 | 3.512641 | 8.848485 | 1.000000 | 0.540000 |
| feasible_two_sector_50cap_single8 | 0.210983 | 0.259121 | 0.157173 | -0.381511 | -0.204373 | -0.159421 | 0.426057 | 3.368740 | 8.848485 | 0.960000 | 0.510000 |

## Deployment diagnostics
| strategy | avg_names_held | risk_on_avg_names | risk_on_target_stock_weight | risk_on_realized_stock_weight | risk_on_fill_rate | avg_top3_stock_weight | avg_safe_haven_weight | underfilled_month_share | dominant_underfill_reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| center_current | 6.505051 | 8.000000 | 1.000000 | 0.800000 | 0.800000 | 0.198485 | 0.440404 | 0.848485 | sector_cap_binding |
| local_stable_neighbor | 6.505051 | 8.000000 | 1.000000 | 0.640000 | 0.640000 | 0.163333 | 0.534141 | 0.848485 | sector_cap_binding |
| explicit_cash_buffer_control | 8.848485 | 12.000000 | 0.800000 | 0.800000 | 1.000000 | 0.139899 | 0.440404 | 0.262626 | hard_defense_zero_stock |
| feasible_two_sector_50cap | 8.848485 | 12.000000 | 1.000000 | 1.000000 | 1.000000 | 0.169192 | 0.323232 | 0.262626 | hard_defense_zero_stock |
| feasible_two_sector_50cap_single8 | 8.848485 | 12.000000 | 1.000000 | 0.960000 | 0.960000 | 0.163333 | 0.346667 | 0.262626 | hard_defense_zero_stock |

## Main comparison (5 bps)
| strategy | period | CAGR | Max Drawdown | Turnover/Year | Average Names Held | risk_on_realized_stock_weight | beta_vs_qqq | alpha_ann_vs_qqq | Information Ratio vs QQQ | Up Capture vs QQQ | Down Capture vs QQQ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| center_current | Full Sample | 0.230343 | -0.327569 | 2.958865 | 6.397300 | 0.800000 | 0.539390 | 0.129267 | 0.183527 | 0.673009 | 0.132240 |
| center_current | OOS Sample | 0.315572 | -0.184113 | 3.157645 | 6.975610 | 0.800000 | 0.496366 | 0.237251 | 0.795541 | 0.980646 | 0.177864 |
| center_current | 2022 | -0.115469 | -0.147348 | 2.124723 | 2.517928 | 0.800000 | 0.136272 | -0.068432 | 0.771918 | -0.001121 | 0.185038 |
| center_current | 2023+ | 0.486063 | -0.184113 | 3.482975 | 8.348466 | 0.800000 | 0.782711 | 0.216227 | 0.832948 | 1.198038 | 0.165374 |
| local_stable_neighbor | Full Sample | 0.200474 | -0.268360 | 2.364666 | 6.397300 | 0.640000 | 0.465862 | 0.111359 | 0.038769 | 0.572352 | 0.096309 |
| local_stable_neighbor | OOS Sample | 0.284838 | -0.163777 | 2.507265 | 6.975610 | 0.640000 | 0.424920 | 0.215848 | 0.697275 | 0.851084 | 0.115986 |
| local_stable_neighbor | 2022 | -0.097298 | -0.119379 | 1.800956 | 2.517928 | 0.640000 | 0.123990 | -0.053794 | 0.835961 | -0.001121 | 0.155305 |
| local_stable_neighbor | 2023+ | 0.431919 | -0.163777 | 2.730899 | 8.348466 | 0.640000 | 0.663949 | 0.202718 | 0.660841 | 1.039787 | 0.047529 |
| explicit_cash_buffer_control | Full Sample | 0.192073 | -0.316997 | 2.710271 | 8.736741 | 0.800000 | 0.515032 | 0.098011 | 0.019684 | 0.608764 | 0.192399 |
| explicit_cash_buffer_control | OOS Sample | 0.249317 | -0.177354 | 3.083024 | 9.151970 | 0.800000 | 0.465801 | 0.184800 | 0.572478 | 0.839606 | 0.214320 |
| explicit_cash_buffer_control | 2022 | -0.128817 | -0.158339 | 2.023546 | 2.820717 | 0.800000 | 0.141147 | -0.081254 | 0.722416 | -0.001121 | 0.206863 |
| explicit_cash_buffer_control | 2023+ | 0.395387 | -0.177354 | 3.416192 | 11.101840 | 0.800000 | 0.724145 | 0.162856 | 0.523446 | 1.025767 | 0.227301 |
| feasible_two_sector_50cap | Full Sample | 0.216009 | -0.394470 | 3.512641 | 8.736741 | 1.000000 | 0.606854 | 0.109926 | 0.149794 | 0.718504 | 0.263392 |
| feasible_two_sector_50cap | OOS Sample | 0.262836 | -0.212659 | 4.072734 | 9.151970 | 1.000000 | 0.552782 | 0.193036 | 0.616167 | 0.960470 | 0.315442 |
| feasible_two_sector_50cap | 2022 | -0.166166 | -0.194591 | 2.580021 | 2.820717 | 1.000000 | 0.166107 | -0.113176 | 0.583428 | -0.001121 | 0.270087 |
| feasible_two_sector_50cap | 2023+ | 0.434188 | -0.200600 | 4.541224 | 11.101840 | 1.000000 | 0.860682 | 0.163561 | 0.649830 | 1.173394 | 0.394405 |
| feasible_two_sector_50cap_single8 | Full Sample | 0.210983 | -0.381511 | 3.368740 | 8.736741 | 0.960000 | 0.589704 | 0.107064 | 0.124937 | 0.696576 | 0.250641 |
| feasible_two_sector_50cap_single8 | OOS Sample | 0.259121 | -0.204373 | 3.902284 | 9.151970 | 0.960000 | 0.536478 | 0.190408 | 0.605176 | 0.935284 | 0.297651 |
| feasible_two_sector_50cap_single8 | 2022 | -0.161138 | -0.187443 | 2.499079 | 2.820717 | 0.960000 | 0.162793 | -0.108873 | 0.602887 | -0.001121 | 0.261738 |
| feasible_two_sector_50cap_single8 | 2023+ | 0.426057 | -0.195459 | 4.342930 | 11.101840 | 0.960000 | 0.833971 | 0.162974 | 0.626716 | 1.142631 | 0.360178 |
| qqq_plus_current_default | Full Sample | 0.241651 | -0.323779 | 3.440286 | 9.465767 | 0.960000 | 0.680133 | 0.126004 | 0.259556 | 0.803347 | 0.303760 |
| qqq_plus_current_default | OOS Sample | 0.334010 | -0.323779 | 3.706698 | 10.570356 | 0.960000 | 0.744797 | 0.241211 | 0.836495 | 1.200446 | 0.399330 |
| qqq_plus_current_default | 2022 | -0.015491 | -0.163557 | 2.893670 | 5.928287 | 0.960000 | 0.208618 | 0.068668 | 1.170392 | 0.228046 | 0.108972 |
| qqq_plus_current_default | 2023+ | 0.464792 | -0.323779 | 3.966892 | 12.000000 | 0.960000 | 1.176538 | 0.121422 | 0.714970 | 1.415764 | 0.904850 |
| aggressive_alt_spec | Full Sample | 0.268277 | -0.408751 | 3.532852 | 10.322083 | 1.000000 | 0.828586 | 0.127691 | 0.379497 | 0.936444 | 0.410912 |
| aggressive_alt_spec | OOS Sample | 0.338226 | -0.340458 | 3.844944 | 12.000000 | 1.000000 | 0.834849 | 0.238178 | 0.868592 | 1.304985 | 0.512384 |
| aggressive_alt_spec | 2022 | -0.024552 | -0.159130 | 2.782375 | 12.000000 | 1.000000 | 0.378451 | 0.129999 | 1.265347 | 0.829437 | 0.354271 |
| aggressive_alt_spec | 2023+ | 0.474999 | -0.340458 | 4.181624 | 12.000000 | 1.000000 | 1.203085 | 0.124098 | 0.733801 | 1.410285 | 0.787663 |
| defensive_baseline | Full Sample | 0.163311 | -0.276198 | 3.926458 | 20.644166 | 1.000000 | 0.465098 | 0.081735 | -0.104102 | 0.595210 | 0.293529 |
| defensive_baseline | OOS Sample | 0.184469 | -0.254393 | 4.530278 | 24.000000 | 1.000000 | 0.568918 | 0.122988 | 0.344425 | 0.826915 | 0.413046 |
| defensive_baseline | 2022 | -0.141182 | -0.171444 | 3.330419 | 24.000000 | 1.000000 | 0.196777 | -0.076385 | 0.723841 | 0.192468 | 0.308680 |
| defensive_baseline | 2023+ | 0.307094 | -0.254393 | 4.911097 | 24.000000 | 1.000000 | 0.867060 | 0.064774 | 0.169337 | 0.967400 | 0.594750 |
| QQQ | Full Sample | 0.179222 | -0.351187 | 0.000000 | 1.000000 | 1.000000 | 1.000000 | 0.000000 |  | 1.000000 | 1.000000 |
| QQQ | OOS Sample | 0.101948 | -0.348280 | 0.000000 | 1.000000 | 1.000000 | 1.000000 | 0.000000 |  | 1.000000 | 1.000000 |
| QQQ | 2022 | -0.328892 | -0.348280 | 0.000000 | 1.000000 | 1.000000 | 1.000000 | -0.000000 |  | 1.000000 | 1.000000 |
| QQQ | 2023+ | 0.282076 | -0.227683 | 0.000000 | 1.000000 | 1.000000 | 1.000000 | 0.000000 |  | 1.000000 | 1.000000 |

## Recommendation
- research_recommendation=research_default_candidate
- role_vs_qqq_plus_current_default=并行分支
- selected_geometry_repair_default=feasible_two_sector_50cap
- reason=geometry-repaired version still has edge, but occupancy or concentration still needs more tightening