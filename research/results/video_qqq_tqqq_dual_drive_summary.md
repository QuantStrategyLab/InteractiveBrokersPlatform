# Video QQQ/TQQQ Dual-Drive Reconstruction

## Setup
- Data: Yahoo Finance adjusted daily OHLCV via the existing research loader.
- Main comparison window follows the video window as closely as trading days allow.
- Cost focus: 5 bps one-way turnover cost.
- The exact video code is not public, so variants are explicit approximations.

## 5 bps Comparison
| strategy | execution_mode | CAGR | Max Drawdown | 2020 Return | 2022 Return | 2023 Return | Turnover/Year | Average QQQ Weight | Average TQQQ Weight | known_limitation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| video_like_same_close_lookahead | same_close_lookahead | 0.414960 | -0.238003 | 1.130881 | -0.081932 | 0.692347 | 2.719947 | 0.367146 | 0.367146 | Biased lookahead control; not implementable as stated. |
| TQQQ_buy_hold | buy_hold | 0.377678 | -0.816599 | 1.100519 | -0.790900 | 1.980486 | 0.000000 | 0.000000 | 1.000000 | Reference only. |
| video_like_pullback_next_close | next_close | 0.339629 | -0.314770 | 0.796966 | -0.116838 | 0.667092 | 3.885638 | 0.387811 | 0.387811 | Approximate reconstruction; exact video state machine and high-exit logic are not public. |
| hybrid_tqqq::baseline::qqq |  | 0.331221 | -0.452355 | 0.744483 | -0.387160 | 0.846587 | 1.381065 | 0.519896 | 0.430104 |  |
| buy_hold_45_45_10 | buy_hold | 0.296118 | -0.589118 | 0.810858 | -0.558120 | 1.029582 | 0.000000 | 0.450000 | 0.450000 | Daily rebalanced reference, not a disclosed video state machine. |
| video_like_overheat_next_close | next_close | 0.279509 | -0.316426 | 0.800494 | -0.209692 | 0.510220 | 2.719947 | 0.367146 | 0.367146 | Approximate reconstruction; exact video state machine and high-exit logic are not public. |
| video_like_next_close | next_close | 0.279509 | -0.316426 | 0.800494 | -0.209692 | 0.510220 | 2.719947 | 0.367146 | 0.367146 | Approximate reconstruction; exact video state machine and high-exit logic are not public. |
| video_like_no_slope_next_close | next_close | 0.257004 | -0.380455 | 0.659450 | -0.287483 | 0.646723 | 3.885638 | 0.371781 | 0.371781 | Approximate reconstruction; exact video state machine and high-exit logic are not public. |
| hybrid_tqqq::baseline::boxx |  | 0.251353 | -0.374023 | 0.445286 | -0.157553 | 0.456001 | 1.455308 | 0.000000 | 0.446725 |  |
| QQQ_buy_hold | buy_hold | 0.201805 | -0.351187 | 0.484061 | -0.325770 | 0.548556 | 0.000000 | 1.000000 | 0.000000 | Reference only. |
| tqqq_growth_income_runtime_dual_drive_tqqq_active_100qqq | next_close | 0.145624 | -0.210548 | 0.199782 | -0.104670 | 0.276365 | 0.712334 | 0.138333 | 0.175942 | Research-only candidate; not enabled in live defaults. |
| tqqq_growth_income_runtime_dual_drive_ma200_50qqq | next_close | 0.137990 | -0.186683 | 0.209425 | -0.094446 | 0.259888 | 1.054626 | 0.069367 | 0.176594 | Research-only candidate; not enabled in live defaults. |
| tqqq_growth_income_runtime_dual_drive_tqqq_active_50qqq | next_close | 0.136288 | -0.192040 | 0.187016 | -0.086814 | 0.250731 | 0.578230 | 0.068932 | 0.176411 | Research-only candidate; not enabled in live defaults. |
| tqqq_growth_income_runtime_dual_drive_ma200_slope_50qqq | next_close | 0.135705 | -0.179503 | 0.213281 | -0.080879 | 0.247747 | 1.993187 | 0.052413 | 0.177250 | Research-only candidate; not enabled in live defaults. |
| tqqq_growth_income_runtime_full_reference |  | 0.127749 | -0.177555 | 0.179046 | -0.072176 | 0.226647 | 0.464928 | 0.000000 | 0.178971 |  |

## Video Reported Reference
- Reported CAGR: 49.40%
- Reported MaxDD: -36.10%
- Reported 2022 return: -15.80%

## Findings
- Best implementable reconstruction is `video_like_pullback_next_close` at 33.96% CAGR / -31.48% MaxDD, well below the video's reported 49.40% CAGR.
- The simple 45/45/10 daily-rebalanced reference is 29.61% CAGR with -58.91% MaxDD, so the video headline is not explained by static QQQ/TQQQ exposure alone.
- The intentionally biased same-close version reaches 41.50% CAGR with -23.80% MaxDD; if a backtest applies close-generated signals to the same close-to-close return, the headline can be inflated.
- Closest CAGR to the video in this local run is `video_like_same_close_lookahead` at 41.50%; it still misses the reported CAGR by 7.90%.
- Raw TQQQ buy-and-hold produces 37.77% CAGR but -81.66% MaxDD, so the video's combination of near-TQQQ-level CAGR and much lower drawdown needs exact state-machine disclosure before trusting it.
- Highest-CAGR video-inspired upgrade for the current full strategy is `tqqq_growth_income_runtime_dual_drive_tqqq_active_100qqq`: 14.56% CAGR / -21.05% MaxDD versus the original 12.77% CAGR / -17.76% MaxDD.
- Most conservative upgrade that keeps drawdown and 2022 close to the original is `tqqq_growth_income_runtime_dual_drive_ma200_slope_50qqq`: 13.57% CAGR / -17.95% MaxDD / 2022 -8.09%.

## Caveats
- The video mentions six internal states, high-level top escape, and below-MA200 low-buy/high-sell behavior, but does not disclose exact conditions.
- The same-close variant is intentionally non-tradable; it is included only as a bias diagnostic.
- BOXX, SPYI, and QQQI histories are shorter than QQQ/TQQQ, matching the existing local research limitation.
