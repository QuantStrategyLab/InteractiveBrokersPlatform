# Stock strategy comparison research

## Full-strategy comparison (5 bps one-way)
| display_name | CAGR | Total Return | Max Drawdown | Sharpe | Sortino | Calmar | Turnover/Year | Average Names Held | Beta vs QQQ | Information Ratio vs QQQ | 2022 Return | 2023+ CAGR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| defensive_baseline | 0.163311 | 2.481396 | -0.276198 | 0.854919 | 1.028644 | 0.591282 | 3.926458 | 20.644166 | 0.465098 | -0.104102 | -0.139659 | 0.307094 |
| hybrid_growth_income::full | 0.115735 | 1.467214 | -0.189709 | 0.881342 | 0.983083 | 0.610064 | 0.554248 | 2.700579 | 0.410286 | -0.444484 | -0.077246 | 0.169747 |
| semiconductor_rotation_income::full | 0.213824 | 3.942889 | -0.331896 | 0.789682 | 1.081284 | 0.644248 | 3.214212 | 3.000000 | 0.937287 | 0.226552 | -0.249901 | 0.342219 |
| qqq_plus_stock_alpha_v1::regime_qqq_breadth | 0.201400 | 3.540848 | -0.373027 | 0.822800 | 0.973531 | 0.539908 | 3.571252 | 13.762777 | 0.722446 | 0.121524 | -0.126033 | 0.337210 |

## Normalized comparison (5 bps one-way)
| display_name | CAGR | Total Return | Max Drawdown | Sharpe | Sortino | Calmar | Turnover/Year | Average Names Held | Beta vs QQQ | Information Ratio vs QQQ | 2022 Return | 2023+ CAGR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| defensive_baseline | 0.163311 | 2.481396 | -0.276198 | 0.854919 | 1.028644 | 0.591282 | 3.926458 | 20.644166 | 0.465098 | -0.104102 | -0.139659 | 0.307094 |
| hybrid_growth_income::no_income | 0.210708 | 3.839246 | -0.454508 | 0.824255 | 0.851149 | 0.463596 | 1.273607 | 0.700579 | 0.781583 | 0.174313 | -0.161719 | 0.211531 |
| semiconductor_rotation_income::no_income | 0.255033 | 5.509552 | -0.399125 | 0.790577 | 1.085816 | 0.638981 | 4.091512 | 1.000000 | 1.161831 | 0.405664 | -0.311252 | 0.403936 |
| qqq_plus_stock_alpha_v1::regime_qqq_breadth | 0.201400 | 3.540848 | -0.373027 | 0.822800 | 0.973531 | 0.539908 | 3.571252 | 13.762777 | 0.722446 | 0.121524 | -0.126033 | 0.337210 |

## Top offensive ablations (5 bps one-way, full sample)
| strategy | universe_filter | holdings_count | single_name_cap | sector_cap | regime_name | soft_defense_exposure | hard_defense_exposure | CAGR | Sharpe | Information Ratio vs QQQ | 2022 Return | 2023+ CAGR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| regime_qqq_breadth | leadership_liquid | 16 | 0.080000 | 0.300000 | qqq_breadth | 0.600000 | 0.000000 | 0.201400 | 0.822800 | 0.121524 | -0.126033 | 0.337210 |
| struct_h12_cap10_sector20 | leadership_liquid | 12 | 0.100000 | 0.200000 | qqq_xlk_smh_breadth | 0.600000 | 0.000000 | 0.190231 | 0.796341 | 0.064995 | -0.137899 | 0.381885 |
| regime_spy_breadth | leadership_liquid | 16 | 0.080000 | 0.300000 | spy_breadth | 0.600000 | 0.000000 | 0.189854 | 0.811835 | 0.061063 | -0.160760 | 0.323089 |
| universe_liquid_50m | liquid_50m | 16 | 0.080000 | 0.300000 | qqq_xlk_smh_breadth | 0.600000 | 0.000000 | 0.185805 | 0.780722 | 0.052016 | -0.155906 | 0.389414 |
| struct_h12_cap10_sector30 | leadership_liquid | 12 | 0.100000 | 0.300000 | qqq_xlk_smh_breadth | 0.600000 | 0.000000 | 0.179569 | 0.748754 | 0.035021 | -0.137209 | 0.352710 |
| struct_h12_cap8_sector20 | leadership_liquid | 12 | 0.080000 | 0.200000 | qqq_xlk_smh_breadth | 0.600000 | 0.000000 | 0.183977 | 0.796950 | 0.032539 | -0.134057 | 0.367543 |
| exposure_100_70_20 | leadership_liquid | 16 | 0.080000 | 0.300000 | qqq_xlk_smh_breadth | 0.700000 | 0.200000 | 0.180629 | 0.775737 | 0.027260 | -0.153639 | 0.326932 |
| struct_h12_cap10_sector40 | leadership_liquid | 12 | 0.100000 | 0.400000 | qqq_xlk_smh_breadth | 0.600000 | 0.000000 | 0.175359 | 0.732981 | 0.021710 | -0.137209 | 0.339019 |
| exposure_100_50_10 | leadership_liquid | 16 | 0.080000 | 0.300000 | qqq_xlk_smh_breadth | 0.500000 | 0.100000 | 0.178543 | 0.775209 | 0.015335 | -0.142388 | 0.328028 |
| struct_h12_cap8_sector30 | leadership_liquid | 12 | 0.080000 | 0.300000 | qqq_xlk_smh_breadth | 0.600000 | 0.000000 | 0.173885 | 0.749444 | 0.003504 | -0.133375 | 0.339845 |