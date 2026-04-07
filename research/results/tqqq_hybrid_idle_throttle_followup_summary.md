# TQQQ idle-asset and minimal-throttle follow-up

## Setup
- Attack-only normalization: isolate the TQQQ sleeve and compare idle assets directly.
- Baseline: existing MA200 + ATR staged TQQQ logic.
- Minimal throttle: if QQQ closes below MA60, cut the computed TQQQ target by 50%.
- Idle assets tested: `CASH`, `BOXX`, `QQQ`.

## OOS 2023+ (5 bps)
| throttle | idle_asset | CAGR | Max Drawdown | Information Ratio vs QQQ | Alpha Ann vs QQQ | Turnover/Year | Average TQQQ Weight | Average Idle Asset Weight |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | 0.239466 | -0.201123 | -0.172266 | -0.029705 | 1.241688 | 0.458291 | 0.491709 |
| baseline | CASH | 0.211531 | -0.202525 | -0.338829 | -0.052564 | 1.244173 | 0.458611 | 0.541389 |
| baseline | QQQ | 0.386503 | -0.321120 | 0.751374 | -0.052876 | 1.142013 | 0.456134 | 0.493866 |
| ma60_half | BOXX | 0.223423 | -0.180200 | -0.308919 | -0.010823 | 3.820005 | 0.421460 | 0.528540 |
| ma60_half | CASH | 0.195971 | -0.184140 | -0.473194 | -0.033980 | 3.834373 | 0.422757 | 0.577243 |
| ma60_half | QQQ | 0.373312 | -0.293642 | 0.732935 | -0.041658 | 3.711045 | 0.413514 | 0.536486 |

## 2022 (5 bps)
| throttle | idle_asset | Total Return | 2022 Return | Max Drawdown | Turnover/Year | Average TQQQ Weight | Average Idle Asset Weight |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | -0.161162 | -0.161162 | -0.173794 | 0.512380 | 0.027606 | 0.922394 |
| baseline | CASH | -0.161719 | -0.161719 | -0.173794 | 0.512380 | 0.027606 | 0.972394 |
| baseline | QQQ | -0.387752 | -0.387752 | -0.412978 | 0.505886 | 0.027191 | 0.922809 |
| ma60_half | BOXX | -0.117219 | -0.117219 | -0.131439 | 0.550353 | 0.017447 | 0.932553 |
| ma60_half | CASH | -0.117805 | -0.117805 | -0.131439 | 0.550353 | 0.017447 | 0.982553 |
| ma60_half | QQQ | -0.361779 | -0.361779 | -0.388075 | 0.505886 | 0.015588 | 0.934412 |

## Recommendation
- Keep the baseline. The simple MA60 half-throttle improves 2022 and drawdown only when paired with defensive idle assets, but it gives up too much CAGR and still loses to baseline+QQQ on growth metrics.
