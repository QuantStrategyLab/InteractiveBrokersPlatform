# growth_pullback_systematic_v1 summary

## Workspace mapping
- qqq_plus suite: /Users/lisiyi/Projects/InteractiveBrokersPlatform/research/backtest_stock_alpha_suite.py
- qqq_plus default config: /Users/lisiyi/Projects/InteractiveBrokersPlatform/research/configs/qqq_plus_stock_alpha_v1_1_default.json
- qqq_plus aggressive config: /Users/lisiyi/Projects/InteractiveBrokersPlatform/research/configs/qqq_plus_stock_alpha_v1_1_aggressive.json
- defensive strategy entry: /Users/lisiyi/Projects/UsEquityStrategies/src/us_equity_strategies/strategies/russell_1000_multi_factor_defensive.py
- defensive backtest entry: /Users/lisiyi/Projects/UsEquityStrategies/src/us_equity_strategies/backtests/russell_1000_multi_factor_defensive.py
- new research entry: /Users/lisiyi/Projects/InteractiveBrokersPlatform/research/backtest_growth_pullback_suite.py
- avoided repos: /Users/lisiyi/Projects/BinancePlatform, /Users/lisiyi/Projects/CryptoLeaderRotation

## Family best table (5 bps)
| family | strategy | robustness_score | full_cagr | oos_cagr | oos_cagr_minus_qqq | return_2022 | cagr_2023_plus |
| --- | --- | --- | --- | --- | --- | --- | --- |
| tech_heavy_pullback | tech_heavy_pullback_balanced_focused_qqq_breadth | 0.837500 | 0.230343 | 0.315572 | 0.213623 | -0.114205 | 0.486063 |
| trend_only_control | trend_only_control_focused_qqq_breadth | 0.756354 | 0.231518 | 0.260316 | 0.158368 | -0.077248 | 0.387365 |
| broad_growth_leadership_pullback | broad_large_cap_eligible_balanced_pullback_balanced_qqq_breadth | 0.735729 | 0.187688 | 0.244492 | 0.142544 | -0.145879 | 0.397523 |
| crypto_equity_theme_pullback | crypto_theme_pullback_balanced_diversified_qqq_breadth | 0.491458 | 0.067517 | 0.092784 | -0.009164 | -0.052431 | 0.141859 |
| naive_dip_buy_control | naive_dip_buy_control_diversified_qqq_breadth | 0.101042 | 0.005111 | -0.090263 | -0.192212 | -0.252505 | -0.033463 |

## Final comparison (5 bps)
| strategy | period | family | CAGR | Total Return | Max Drawdown | Sharpe | Alpha_ann_vs_QQQ | Information Ratio vs QQQ | Turnover/Year |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| broad_large_cap_eligible_balanced_pullback_balanced_qqq_breadth | Full Sample | broad_growth_leadership_pullback | 0.187688 | 3.130730 | -0.336270 | 0.817945 | 0.084731 | 0.043487 | 3.765276 |
| broad_large_cap_eligible_balanced_pullback_balanced_qqq_breadth | OOS Sample | broad_growth_leadership_pullback | 0.244492 | 1.529967 | -0.298224 | 0.966691 | 0.170852 | 0.574623 | 4.170919 |
| broad_large_cap_eligible_balanced_pullback_balanced_qqq_breadth | 2022 | broad_growth_leadership_pullback | -0.147463 | -0.145879 | -0.169330 | -1.163525 | -0.080481 | 0.694748 | 3.085907 |
| broad_large_cap_eligible_balanced_pullback_balanced_qqq_breadth | 2023+ | broad_growth_leadership_pullback | 0.397523 | 1.962072 | -0.298224 | 1.298357 | 0.098472 | 0.532374 | 4.515538 |
| tech_heavy_pullback_balanced_focused_qqq_breadth | Full Sample | tech_heavy_pullback | 0.230343 | 4.525776 | -0.327569 | 1.025487 | 0.129267 | 0.183527 | 2.958865 |
| tech_heavy_pullback_balanced_focused_qqq_breadth | OOS Sample | tech_heavy_pullback | 0.315572 | 2.202451 | -0.184113 | 1.369692 | 0.237251 | 0.795541 | 3.157645 |
| tech_heavy_pullback_balanced_focused_qqq_breadth | 2022 | tech_heavy_pullback | -0.115469 | -0.114205 | -0.147348 | -1.017588 | -0.068432 | 0.771919 | 2.124723 |
| tech_heavy_pullback_balanced_focused_qqq_breadth | 2023+ | tech_heavy_pullback | 0.486063 | 2.615342 | -0.184113 | 1.769672 | 0.216227 | 0.832948 | 3.482975 |
| crypto_theme_pullback_balanced_diversified_qqq_breadth | Full Sample | crypto_equity_theme_pullback | 0.067517 | 0.713920 | -0.157602 | 0.762665 | 0.028227 | -0.613213 | 0.603899 |
| crypto_theme_pullback_balanced_diversified_qqq_breadth | OOS Sample | crypto_equity_theme_pullback | 0.092784 | 0.457226 | -0.157602 | 0.880941 | 0.065088 | -0.149050 | 0.933155 |
| crypto_theme_pullback_balanced_diversified_qqq_breadth | 2022 | crypto_equity_theme_pullback | -0.053031 | -0.052431 | -0.079751 | -0.797088 | -0.027924 | 0.958429 | 1.153421 |
| crypto_theme_pullback_balanced_diversified_qqq_breadth | 2023+ | crypto_equity_theme_pullback | 0.141859 | 0.537856 | -0.157602 | 1.191519 | 0.039602 | -0.836297 | 0.869203 |
| trend_only_control_focused_qqq_breadth | Full Sample | trend_only_control | 0.231518 | 4.569430 | -0.410980 | 0.872556 | 0.105094 | 0.242711 | 3.787508 |
| trend_only_control_focused_qqq_breadth | OOS Sample | trend_only_control | 0.260316 | 1.669329 | -0.325144 | 0.965348 | 0.180615 | 0.634821 | 4.088444 |
| trend_only_control_focused_qqq_breadth | 2022 | trend_only_control | -0.078121 | -0.077248 | -0.160871 | -0.397697 | 0.048189 | 1.111217 | 3.912188 |
| trend_only_control_focused_qqq_breadth | 2023+ | trend_only_control | 0.387365 | 1.892791 | -0.325144 | 1.220695 | 0.092491 | 0.474089 | 4.155939 |
| naive_dip_buy_control_diversified_qqq_breadth | Full Sample | naive_dip_buy_control | 0.005111 | 0.042939 | -0.470992 | 0.146074 | -0.076287 | -0.676548 | 6.493738 |
| naive_dip_buy_control_diversified_qqq_breadth | OOS Sample | naive_dip_buy_control | -0.090263 | -0.330652 | -0.358560 | -0.219059 | -0.143448 | -0.799469 | 7.130230 |
| naive_dip_buy_control_diversified_qqq_breadth | 2022 | naive_dip_buy_control | -0.255062 | -0.252505 | -0.289186 | -0.984975 | -0.095685 | 0.315129 | 3.768854 |
| naive_dip_buy_control_diversified_qqq_breadth | 2023+ | naive_dip_buy_control | -0.033463 | -0.104545 | -0.294399 | 0.009105 | -0.225370 | -1.243968 | 8.178312 |
| qqq_plus_current_default | Full Sample | reference | 0.241651 | 4.958780 | -0.323779 | 0.903840 | 0.126004 | 0.259555 | 3.440286 |
| qqq_plus_current_default | OOS Sample | reference | 0.334010 | 2.397303 | -0.323779 | 1.117224 | 0.241211 | 0.836495 | 3.706698 |
| qqq_plus_current_default | 2022 | reference | -0.015491 | -0.015312 | -0.163557 | -0.019576 | 0.068668 | 1.170392 | 2.893670 |
| qqq_plus_current_default | 2023+ | reference | 0.464792 | 2.450132 | -0.323779 | 1.325631 | 0.121422 | 0.714970 | 3.966892 |
| aggressive_alt_spec | Full Sample | reference | 0.268277 | 6.098166 | -0.408751 | 0.914116 | 0.127691 | 0.379497 | 3.532852 |
| aggressive_alt_spec | OOS Sample | reference | 0.338226 | 2.443099 | -0.340458 | 1.085907 | 0.238178 | 0.868593 | 3.844944 |
| aggressive_alt_spec | 2022 | reference | -0.024552 | -0.024270 | -0.159130 | -0.000751 | 0.129999 | 1.265348 | 2.782375 |
| aggressive_alt_spec | 2023+ | reference | 0.474999 | 2.528740 | -0.340458 | 1.321844 | 0.124099 | 0.733801 | 4.181624 |
| defensive_baseline | Full Sample | reference | 0.163311 | 2.481396 | -0.276198 | 0.854919 | 0.081736 | -0.104102 | 3.926458 |
| defensive_baseline | OOS Sample | reference | 0.184469 | 1.051214 | -0.254393 | 0.885560 | 0.122988 | 0.344425 | 4.530278 |
| defensive_baseline | 2022 | reference | -0.141182 | -0.139659 | -0.171444 | -1.225741 | -0.076385 | 0.723842 | 3.330419 |
| defensive_baseline | 2023+ | reference | 0.307094 | 1.384188 | -0.254393 | 1.234418 | 0.064774 | 0.169337 | 4.911097 |
| QQQ | Full Sample | reference | 0.179222 | 2.894099 | -0.351187 | 0.811457 | 0.000000 |  | 0.000000 |
| QQQ | OOS Sample | reference | 0.101948 | 0.509796 | -0.348280 | 0.533389 | 0.000000 |  | 0.000000 |
| QQQ | 2022 | reference | -0.328892 | -0.325770 | -0.348280 | -1.071930 | 0.000000 |  | 0.000000 |
| QQQ | 2023+ | reference | 0.282076 | 1.239290 | -0.227683 | 1.355202 | 0.000000 |  | 0.000000 |

## Crypto-linked theme list
| symbol | theme_bucket | reason | present_in_iwb_proxy | first_seen_start_date |
| --- | --- | --- | --- | --- |
| COIN | exchange_broker | Coinbase: spot + institutional crypto exchange / broker | True | 2022-06-30 |
| MSTR | btc_treasury_proxy | MicroStrategy/Strategy: listed BTC treasury proxy | True | 2024-06-28 |
| HOOD | broker | Robinhood: retail broker with material crypto trading exposure | True | 2022-06-30 |
| SQ | payments_wallet | Block historical ticker SQ: Cash App / bitcoin exposure | True | 2018-01-31 |
| XYZ | payments_wallet | Block current ticker XYZ: Cash App / bitcoin exposure | True | 2025-01-31 |
| PYPL | payments_wallet | PayPal: crypto trading / wallet / stablecoin related initiatives | True | 2018-01-31 |
| IBKR | broker | Interactive Brokers: access rails for crypto trading | True | 2018-01-31 |
| CME | exchange_infra | CME: listed crypto futures / institutional market infrastructure | True | 2018-01-31 |
| CBOE | exchange_infra | Cboe: crypto ETF / options market structure exposure | True | 2018-01-31 |

## Recommendation
- default_research_spec=tech_heavy_pullback_balanced_focused_qqq_breadth
- aggressive_research_spec=trend_only_control_focused_qqq_breadth
- stock_line_to_continue=tech_heavy_pullback
- crypto_linked_equity_theme=no
- next_main_direction=continue_stock_line_spec_lock