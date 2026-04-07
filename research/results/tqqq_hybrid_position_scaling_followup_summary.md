# TQQQ position-scaling follow-up

## Setup
- Keep the existing MA200 + ATR TQQQ entry/exit framework.
- Change only the TQQQ position size while already invested.
- Compare two idle assets: `BOXX` and `QQQ`.
- Goal: see whether smoother in-position scaling creates a smoother equity curve without obviously killing CAGR.

## OOS 2023+ (5 bps)
| scaling | idle_asset | CAGR | Max Drawdown | Ulcer Index | Information Ratio vs QQQ | Alpha Ann vs QQQ | Turnover/Year | Average TQQQ Weight | Average Scale While Invested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | 0.239466 | -0.201122 | 7.500255 | -0.172266 | -0.029705 | 1.241687 | 0.458291 | 1.000000 |
| baseline | QQQ | 0.386503 | -0.321120 | 9.612988 | 0.751374 | -0.052876 | 1.142013 | 0.456134 | 1.000000 |
| consensus_score_6 | BOXX | 0.156839 | -0.156063 | 6.923503 | -0.793039 | -0.027800 | 8.259294 | 0.356474 | 0.767612 |
| consensus_score_6 | QQQ | 0.329967 | -0.300120 | 9.125674 | 0.525343 | -0.052009 | 8.130003 | 0.352832 | 0.767612 |
| overheat_trim | BOXX | 0.158150 | -0.167014 | 7.046143 | -0.783704 | -0.026343 | 8.819797 | 0.358623 | 0.777045 |
| overheat_trim | QQQ | 0.330944 | -0.305921 | 9.154724 | 0.528886 | -0.051768 | 8.743529 | 0.357008 | 0.777045 |
| trend_score_4_boost | BOXX | 0.156112 | -0.183933 | 7.957516 | -0.751608 | -0.052386 | 9.150995 | 0.407473 | 0.883509 |
| trend_score_4_boost | QQQ | 0.327090 | -0.315606 | 9.779819 | 0.481955 | -0.068955 | 9.082261 | 0.405374 | 0.883509 |

## Full Sample (5 bps)
| scaling | idle_asset | CAGR | Max Drawdown | Ulcer Index | Information Ratio vs QQQ | Turnover/Year | Average TQQQ Weight | Average Scale While Invested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | 0.221714 | -0.454508 | 12.311460 | 0.216285 | 1.272629 | 0.394916 | 1.000000 |
| baseline | QQQ | 0.284186 | -0.499827 | 17.100177 | 0.746534 | 1.242602 | 0.378650 | 1.000000 |
| consensus_score_6 | BOXX | 0.183127 | -0.263008 | 9.198819 | -0.027300 | 6.625118 | 0.309396 | 0.778286 |
| consensus_score_6 | QQQ | 0.266136 | -0.405291 | 15.497018 | 0.804185 | 6.351841 | 0.294080 | 0.778286 |
| overheat_trim | BOXX | 0.195924 | -0.265233 | 9.773856 | 0.021091 | 6.763046 | 0.306017 | 0.785444 |
| overheat_trim | QQQ | 0.275458 | -0.415341 | 16.042876 | 0.874121 | 6.583965 | 0.295195 | 0.785444 |
| trend_score_4_boost | BOXX | 0.192405 | -0.320974 | 10.956462 | 0.042118 | 7.243384 | 0.353307 | 0.899759 |
| trend_score_4_boost | QQQ | 0.270482 | -0.421216 | 16.574493 | 0.760466 | 7.029861 | 0.339469 | 0.899759 |

## 2022 (5 bps)
| scaling | idle_asset | Total Return | 2022 Return | Max Drawdown | Ulcer Index | Turnover/Year |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | -0.161162 | -0.161162 | -0.173794 | 17.021365 | 0.512380 |
| baseline | QQQ | -0.387752 | -0.387752 | -0.412978 | 31.478236 | 0.505886 |
| consensus_score_6 | BOXX | -0.084095 | -0.084095 | -0.095793 | 9.419646 | 0.430003 |
| consensus_score_6 | QQQ | -0.350285 | -0.350285 | -0.376171 | 27.465834 | 0.430003 |
| overheat_trim | BOXX | -0.089552 | -0.089552 | -0.103098 | 10.151397 | 0.505886 |
| overheat_trim | QQQ | -0.353839 | -0.353839 | -0.380462 | 27.937527 | 0.505886 |
| trend_score_4_boost | BOXX | -0.108780 | -0.108780 | -0.122007 | 11.991866 | 0.733535 |
| trend_score_4_boost | QQQ | -0.363123 | -0.363123 | -0.389342 | 28.902489 | 0.733535 |

## Recommendation
- No tested scaling rule clearly upgrades the current baseline. The score-based variants smooth drawdown a bit, but the gain mostly comes from carrying less TQQQ and paying materially more turnover.
