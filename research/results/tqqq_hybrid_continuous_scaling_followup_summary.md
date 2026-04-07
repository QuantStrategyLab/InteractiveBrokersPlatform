# TQQQ continuous position-scaling follow-up

## Setup
- Keep the existing MA200 + ATR TQQQ entry/exit framework.
- Change only the TQQQ position size while already invested.
- Use one continuous indicator only: QQQ distance vs MA20.
- Compare two idle assets: `BOXX` and `QQQ`.

## OOS 2023+ (5 bps)
| scaling | idle_asset | CAGR | Max Drawdown | Ulcer Index | Information Ratio vs QQQ | Alpha Ann vs QQQ | Turnover/Year | Average TQQQ Weight | Average Scale While Invested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | 0.238602 | -0.201123 | 7.514041 | -0.187183 | -0.031695 | 1.237510 | 0.457729 | 1.000000 |
| baseline | QQQ | 0.387703 | -0.321120 | 9.627919 | 0.750230 | -0.054166 | 1.138170 | 0.455575 | 1.000000 |
| ma20_gap_linear | BOXX | 0.210636 | -0.199751 | 8.330539 | -0.353209 | -0.050267 | 4.403525 | 0.467389 | 1.020293 |
| ma20_gap_linear | QQQ | 0.365982 | -0.326197 | 10.136864 | 0.645963 | -0.067824 | 4.436067 | 0.466023 | 1.020293 |
| ma20_gap_trim_only | BOXX | 0.218393 | -0.182403 | 7.528751 | -0.341663 | -0.035422 | 2.910826 | 0.442867 | 0.965488 |
| ma20_gap_trim_only | QQQ | 0.372159 | -0.317635 | 9.637568 | 0.695452 | -0.057661 | 2.863260 | 0.440277 | 0.965488 |

## Full Sample (5 bps)
| scaling | idle_asset | CAGR | Max Drawdown | Ulcer Index | Information Ratio vs QQQ | Turnover/Year | Average TQQQ Weight | Average Scale While Invested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | 0.221402 | -0.454508 | 12.312903 | 0.212472 | 1.270941 | 0.394726 | 1.000000 |
| baseline | QQQ | 0.284755 | -0.499827 | 17.100661 | 0.746109 | 1.240954 | 0.378468 | 1.000000 |
| ma20_gap_linear | BOXX | 0.219139 | -0.394992 | 11.705114 | 0.188906 | 3.749817 | 0.400119 | 1.022352 |
| ma20_gap_linear | QQQ | 0.285719 | -0.461445 | 16.880328 | 0.768228 | 3.672616 | 0.385694 | 1.022352 |
| ma20_gap_trim_only | BOXX | 0.214930 | -0.387385 | 11.232493 | 0.164054 | 2.596423 | 0.379484 | 0.962610 |
| ma20_gap_trim_only | QQQ | 0.283283 | -0.458986 | 16.580457 | 0.779122 | 2.568179 | 0.363742 | 0.962610 |

## 2022 (5 bps)
| scaling | idle_asset | Total Return | 2022 Return | Max Drawdown | Ulcer Index | Turnover/Year |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | -0.161162 | -0.161162 | -0.173794 | 17.021359 | 0.512380 |
| baseline | QQQ | -0.387752 | -0.387752 | -0.412978 | 31.478228 | 0.505886 |
| ma20_gap_linear | BOXX | -0.136642 | -0.136642 | -0.149992 | 14.721060 | 0.742490 |
| ma20_gap_linear | QQQ | -0.376827 | -0.376827 | -0.402736 | 30.365284 | 0.742490 |
| ma20_gap_trim_only | BOXX | -0.136243 | -0.136243 | -0.149094 | 14.629202 | 0.670105 |
| ma20_gap_trim_only | QQQ | -0.376170 | -0.376170 | -0.401873 | 30.269567 | 0.670105 |

## Recommendation
- The trim-only continuous mapping is cleaner than the earlier score-based variants, but it still does not clearly beat the baseline. It smooths the path mainly by carrying a bit less TQQQ, not by creating a meaningfully better OOS growth profile.
