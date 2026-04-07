# TQQQ / hybrid_growth_income indicator overlay review

## Setup
- Baseline signal: current QQQ MA200 + ATR staged TQQQ logic.
- Main research set: attack-only normalization (`income_threshold_usd = 1e9`) so the idle-asset choice is visible.
- Runtime reference: current full `hybrid_growth_income` with income layer on.
- Idle asset candidates: `CASH`, `BOXX`, `QQQ`.
- Extra indicator gates are nested from simple to complex to avoid blind indicator stuffing.

## OOS 2023+ (5 bps)
| overlay | idle_asset | CAGR | Max Drawdown | Information Ratio vs QQQ | Alpha Ann vs QQQ | 2022 Return | Turnover/Year | Average TQQQ Weight | Average Idle Asset Weight | Gate Active Share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | BOXX | 0.239466 | -0.201123 | -0.172267 | -0.029705 |  | 1.241688 | 0.458291 | 0.491709 | 1.000000 |
| baseline | CASH | 0.211531 | -0.202525 | -0.338829 | -0.052564 |  | 1.244173 | 0.458611 | 0.541389 | 1.000000 |
| baseline | QQQ | 0.386503 | -0.321120 | 0.751374 | -0.052876 |  | 1.142013 | 0.456134 | 0.493866 | 1.000000 |
| consensus_5 | BOXX | 0.020592 | -0.263393 | -1.134282 | -0.073290 |  | 10.121286 | 0.211529 | 0.738471 | 0.300613 |
| consensus_5 | CASH | -0.016761 | -0.300828 | -1.304935 | -0.111832 |  | 10.339113 | 0.215327 | 0.784673 | 0.300613 |
| consensus_5 | QQQ | 0.249651 | -0.283935 | -0.180582 | -0.056725 |  | 7.355879 | 0.154215 | 0.795785 | 0.300613 |
| consensus_7_weekly_kdj | BOXX | 0.055891 | -0.217319 | -0.996315 | -0.023159 |  | 8.089719 | 0.175798 | 0.774202 | 0.257669 |
| consensus_7_weekly_kdj | CASH | 0.016094 | -0.258748 | -1.178105 | -0.062625 |  | 8.264047 | 0.178938 | 0.821062 | 0.257669 |
| consensus_7_weekly_kdj | QQQ | 0.268117 | -0.278986 | -0.034910 | -0.035649 |  | 6.075503 | 0.132153 | 0.817847 | 0.257669 |
| ma20_ma60_stack | BOXX | 0.107698 | -0.175128 | -0.854381 | -0.019696 |  | 6.554695 | 0.296228 | 0.653772 | 0.571779 |
| ma20_ma60_stack | CASH | 0.077047 | -0.180886 | -1.008867 | -0.048551 |  | 6.628876 | 0.298799 | 0.701201 | 0.571779 |
| ma20_ma60_stack | QQQ | 0.296045 | -0.284608 | 0.257137 | -0.046158 |  | 6.273944 | 0.283060 | 0.666940 | 0.571779 |
| ma_stack_rsi_macd | BOXX | 0.013013 | -0.265125 | -1.182128 | -0.081451 |  | 11.232051 | 0.218740 | 0.731260 | 0.316564 |
| ma_stack_rsi_macd | CASH | -0.023811 | -0.304549 | -1.352885 | -0.119740 |  | 11.473083 | 0.222736 | 0.777264 | 0.316564 |
| ma_stack_rsi_macd | QQQ | 0.243462 | -0.282495 | -0.236261 | -0.062321 |  | 8.232730 | 0.161048 | 0.788952 | 0.316564 |

## Runtime full reference (2023+, 5 bps)
| strategy | idle_asset | CAGR | Max Drawdown | Information Ratio vs QQQ | Alpha Ann vs QQQ | 2022 Return | Turnover/Year |
| --- | --- | --- | --- | --- | --- | --- | --- |
| hybrid_growth_income_runtime_full_reference | BOXX | 0.180025 | -0.177757 | -1.291324 | -0.027152 |  | 0.478999 |

## Recommendation
- The tested overlays mainly trade more often; they do not justify upgrading the default logic yet.
- Best attack-only candidate in this run: `baseline` + `QQQ`
- OOS CAGR: 38.65%
- OOS MaxDD: -32.11%
- OOS IR vs QQQ: 0.751
- 2022: nan%

## Caveats
- BOXX pre-launch history is naturally short; before listed data exists it behaves like 0% carry in this backtest, so early BOXX results are closer to cash than to realized BOXX carry.
- These tests add daily gates on top of the existing daily TQQQ logic; they are not monthly overlays and should not be read as direct runtime recommendations.
- Weekly KDJ is mapped back to daily with forward-fill from Friday weekly bars; useful for a sanity check, but not a reason to trust the heaviest gate by default.
