# growth_pullback_systematic_v1.1 spec lock

## Center spec
- strategy=tech_heavy_pullback_balanced_focused_qqq_breadth
- family=tech_heavy_pullback
- universe=tech_heavy
- normalization=universe_cross_sectional
- holdings=12
- single_cap=10%
- sector_cap=40%
- hold_bonus=0.05
- min_adv20=50M
- regime=qqq_breadth
- exposures=100/60/0

## Occupancy summary
| strategy | avg_selected_count | risk_on_avg_names | soft_defense_avg_names | share_lt_nominal | share_lt_8 | avg_top3_stock_weight | avg_safe_haven_weight | dominant_underfill_reason | dominant_underfill_reason_share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tech_heavy_pullback_balanced_focused_qqq_breadth | 6.505051 | 8.000000 | 12.000000 | 0.848485 | 0.262626 | 0.198485 | 0.440404 | sector_cap_binding | 0.690476 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__holdings_count_16 | 9.454545 | 12.000000 | 16.000000 | 0.848485 | 0.262626 | 0.163510 | 0.323232 | sector_cap_binding | 0.690476 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08 | 6.505051 | 8.000000 | 12.000000 | 0.848485 | 0.262626 | 0.163333 | 0.534141 | sector_cap_binding | 0.690476 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__sector_cap_0p3 | 5.333333 | 6.000000 | 12.000000 | 0.848485 | 0.848485 | 0.198485 | 0.557576 | sector_cap_binding | 0.690476 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__sector_cap_0p5 | 8.848485 | 12.000000 | 12.000000 | 0.262626 | 0.262626 | 0.169192 | 0.323232 | hard_defense_zero_stock | 1.000000 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__hold_bonus_0p1 | 6.505051 | 8.000000 | 12.000000 | 0.848485 | 0.262626 | 0.198485 | 0.440404 | sector_cap_binding | 0.690476 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__adv20m | 6.343434 | 8.000000 | 12.000000 | 0.868687 | 0.272727 | 0.198485 | 0.444444 | sector_cap_binding | 0.686047 |

## Local plateau (5 bps, OOS)
- local_plateau_50bps_share=16.7%
- local_plateau_100bps_share=16.7%
- local_plateau_200bps_share=16.7%
| strategy | variant_scope | change_summary | CAGR | Max Drawdown | Turnover/Year | delta_oos_cagr_vs_center | delta_oos_maxdd_vs_center | delta_oos_turnover_vs_center |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tech_heavy_pullback_balanced_focused_qqq_breadth | center | center | 0.315572 | -0.184113 | 3.157645 | 0.000000 | 0.000000 | 0.000000 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__holdings_count_16 | first_order | holdings_count:12->16 | 0.220996 | -0.209158 | 4.139500 | -0.094576 | -0.025045 | 0.981855 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08 | first_order | single_name_cap:0.1->0.08 | 0.284838 | -0.163777 | 2.507265 | -0.030734 | 0.020336 | -0.650381 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__sector_cap_0p3 | first_order | sector_cap:0.4->0.3 | 0.270292 | -0.185755 | 2.415363 | -0.045280 | -0.001642 | -0.742282 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__sector_cap_0p5 | first_order | sector_cap:0.4->0.5 | 0.262836 | -0.212659 | 4.072734 | -0.052736 | -0.028546 | 0.915089 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__hold_bonus_0p1 | first_order | hold_bonus:0.05->0.1 | 0.319163 | -0.184113 | 3.016258 | 0.003592 | -0.000000 | -0.141387 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__adv20m | first_order | min_adv20_usd:50000000.0->20000000.0 | 0.279641 | -0.198353 | 3.051605 | -0.035931 | -0.014240 | -0.106040 |

## Main comparison (5 bps)
| strategy | period | family | CAGR | Total Return | Max Drawdown | Sharpe | Alpha_ann_vs_QQQ | Information Ratio vs QQQ | Turnover/Year | Average Names Held |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tech_heavy_pullback_balanced_focused_qqq_breadth | Full Sample | tech_heavy_pullback | 0.230343 | 4.525776 | -0.327569 | 1.025487 | 0.129267 | 0.183527 | 2.958865 | 6.397300 |
| tech_heavy_pullback_balanced_focused_qqq_breadth | OOS Sample | tech_heavy_pullback | 0.315572 | 2.202451 | -0.184113 | 1.369692 | 0.237251 | 0.795541 | 3.157645 | 6.975610 |
| tech_heavy_pullback_balanced_focused_qqq_breadth | 2022 | tech_heavy_pullback | -0.115469 | -0.114205 | -0.147348 | -1.017588 | -0.068432 | 0.771918 | 2.124723 | 2.517928 |
| tech_heavy_pullback_balanced_focused_qqq_breadth | 2023+ | tech_heavy_pullback | 0.486063 | 2.615342 | -0.184113 | 1.769672 | 0.216227 | 0.832949 | 3.482975 | 8.348466 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08 | Full Sample | tech_heavy_pullback | 0.200474 | 3.512050 | -0.268360 | 1.049004 | 0.111359 | 0.038769 | 2.364666 | 6.397300 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08 | OOS Sample | tech_heavy_pullback | 0.284838 | 1.896784 | -0.163777 | 1.446512 | 0.215848 | 0.697275 | 2.507265 | 6.975610 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08 | 2022 | tech_heavy_pullback | -0.097298 | -0.096223 | -0.119379 | -0.953259 | -0.053793 | 0.835961 | 1.800956 | 2.517928 |
| tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08 | 2023+ | tech_heavy_pullback | 0.431919 | 2.205196 | -0.163777 | 1.864878 | 0.202718 | 0.660842 | 2.730899 | 8.348466 |
| qqq_plus_current_default | Full Sample | reference | 0.241651 | 4.958780 | -0.323779 | 0.903840 | 0.126004 | 0.259556 | 3.440286 | 9.465767 |
| qqq_plus_current_default | OOS Sample | reference | 0.334010 | 2.397303 | -0.323779 | 1.117224 | 0.241211 | 0.836495 | 3.706698 | 10.570356 |
| qqq_plus_current_default | 2022 | reference | -0.015491 | -0.015312 | -0.163557 | -0.019576 | 0.068668 | 1.170392 | 2.893670 | 5.928287 |
| qqq_plus_current_default | 2023+ | reference | 0.464792 | 2.450132 | -0.323779 | 1.325631 | 0.121422 | 0.714971 | 3.966892 | 12.000000 |
| aggressive_alt_spec | Full Sample | reference | 0.268277 | 6.098166 | -0.408751 | 0.914116 | 0.127691 | 0.379497 | 3.532852 | 10.322083 |
| aggressive_alt_spec | OOS Sample | reference | 0.338226 | 2.443099 | -0.340458 | 1.085907 | 0.238178 | 0.868593 | 3.844944 | 12.000000 |
| aggressive_alt_spec | 2022 | reference | -0.024552 | -0.024270 | -0.159130 | -0.000751 | 0.129999 | 1.265348 | 2.782375 | 12.000000 |
| aggressive_alt_spec | 2023+ | reference | 0.474999 | 2.528740 | -0.340458 | 1.321844 | 0.124098 | 0.733801 | 4.181624 | 12.000000 |
| defensive_baseline | Full Sample | reference | 0.163311 | 2.481396 | -0.276198 | 0.854919 | 0.081736 | -0.104102 | 3.926458 | 20.644166 |
| defensive_baseline | OOS Sample | reference | 0.184469 | 1.051214 | -0.254393 | 0.885560 | 0.122988 | 0.344425 | 4.530278 | 24.000000 |
| defensive_baseline | 2022 | reference | -0.141182 | -0.139659 | -0.171444 | -1.225741 | -0.076385 | 0.723841 | 3.330419 | 24.000000 |
| defensive_baseline | 2023+ | reference | 0.307094 | 1.384188 | -0.254393 | 1.234418 | 0.064774 | 0.169338 | 4.911097 | 24.000000 |
| QQQ | Full Sample | reference | 0.179222 | 2.894099 | -0.351187 | 0.811457 | 0.000000 |  | 0.000000 | 1.000000 |
| QQQ | OOS Sample | reference | 0.101948 | 0.509796 | -0.348280 | 0.533389 | 0.000000 |  | 0.000000 | 1.000000 |
| QQQ | 2022 | reference | -0.328892 | -0.325770 | -0.348280 | -1.071929 | 0.000000 |  | 0.000000 | 1.000000 |
| QQQ | 2023+ | reference | 0.282076 | 1.239290 | -0.227683 | 1.355202 | 0.000000 |  | 0.000000 | 1.000000 |

## Recommendation
- research_recommendation=research_default_candidate
- role_vs_qqq_plus_current_default=并行分支
- selected_local_default=tech_heavy_pullback_balanced_focused_qqq_breadth__single_name_cap_0p08
- reason=edge is good, but occupancy/concentration or local plateau still needs more tightening