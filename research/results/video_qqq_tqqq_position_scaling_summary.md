# Video QQQ/TQQQ Position-Scaling Follow-up

## Setup
- Data: Nasdaq daily close/OHLC, not dividend-adjusted.
- Signal timing: next-close implementation; no same-close lookahead.
- Baseline risk-on sleeve: 45% QQQ + 45% TQQQ + 10% cash.
- Scaling changes only the TQQQ sleeve while already in a risk-on or pullback-risk-on state.

## 5 bps Comparison
| strategy | signal_mode | scaling | CAGR | Max Drawdown | 2020 Return | 2022 Return | 2023 Return | 2023+ CAGR | Turnover/Year | Average QQQ Weight | Average TQQQ Weight | Average Cash Weight | Average Scale While Invested |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pullback_baseline | pullback | baseline | 0.341454 | -0.314772 | 0.794111 | -0.117330 | 0.649612 | 0.419052 | 3.974558 | 0.384090 | 0.384090 | 0.231820 | 1.000000 |
| pullback_trend_score_4 | pullback | trend_score_4 | 0.314650 | -0.285685 | 0.872532 | -0.126186 | 0.592241 | 0.356011 | 6.654961 | 0.384090 | 0.379503 | 0.236407 | 0.988052 |
| pullback_ma20_gap_trim_boost | pullback | ma20_gap_trim_boost | 0.308969 | -0.265040 | 0.854259 | -0.092818 | 0.619772 | 0.367045 | 9.163294 | 0.384090 | 0.362833 | 0.253077 | 0.944553 |
| pullback_ma20_gap_trim_only | pullback | ma20_gap_trim_only | 0.308237 | -0.262396 | 0.836130 | -0.085917 | 0.588322 | 0.358232 | 7.585589 | 0.384090 | 0.350605 | 0.265305 | 0.912776 |
| pullback_ma60_half | pullback | ma60_half | 0.280193 | -0.256890 | 0.720245 | -0.128211 | 0.560757 | 0.365043 | 6.107247 | 0.384090 | 0.352484 | 0.263426 | 0.917671 |
| trend_only_baseline | trend_only | baseline | 0.289016 | -0.313426 | 0.797634 | -0.209692 | 0.494385 | 0.325037 | 2.617392 | 0.362698 | 0.362698 | 0.274604 | 1.000000 |
| trend_only_trend_score_4 | trend_only | trend_score_4 | 0.273785 | -0.287904 | 0.875586 | -0.184498 | 0.468462 | 0.282044 | 5.394735 | 0.362698 | 0.361918 | 0.275384 | 0.997847 |
| trend_only_ma20_gap_trim_only | trend_only | ma20_gap_trim_only | 0.257097 | -0.296058 | 0.839736 | -0.181566 | 0.438862 | 0.268247 | 6.228423 | 0.362698 | 0.329213 | 0.308089 | 0.907629 |
| trend_only_ma20_gap_trim_boost | trend_only | ma20_gap_trim_boost | 0.256182 | -0.303421 | 0.858270 | -0.187168 | 0.456422 | 0.268273 | 7.631635 | 0.362698 | 0.339186 | 0.298116 | 0.935061 |
| trend_only_ma60_half | trend_only | ma60_half | 0.242760 | -0.265419 | 0.722479 | -0.175596 | 0.439420 | 0.291413 | 4.847022 | 0.362698 | 0.334176 | 0.303126 | 0.921318 |

## Findings
- Pullback baseline: 34.15% CAGR / -31.48% MaxDD / turnover 3.97/yr.
- Best scaled pullback candidate: `trend_score_4` at 31.47% CAGR / -28.57% MaxDD / turnover 6.65/yr.
- Scaling smooths drawdown, but it lowers CAGR and adds turnover; this is not a clear upgrade.

## Caveats
- Nasdaq close data is not adjusted for dividends, so absolute CAGR is not a replacement for the retained Yahoo adjusted reference.
- This is a research-only experiment; no live allocation code was changed.
