# Video QQQ/TQQQ Growth Optimization Follow-up

## Setup
- Data: Nasdaq daily close/OHLC, not dividend-adjusted.
- Signal timing: next-close implementation; no same-close lookahead.
- Baseline: retained pullback reconstruction, 45% QQQ + 45% TQQQ + 10% cash.
- Goal: improve growth without treating lower drawdown as the primary objective.

## Top CAGR Candidates
| strategy | theme | CAGR | Max Drawdown | 2020 Return | 2022 Return | 2023 Return | 2023+ CAGR | Turnover/Year | Average QQQ Weight | Average TQQQ Weight | Average Cash Weight |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| attack_25_65 | attack_weight | 0.407796 | -0.374397 | 0.984770 | -0.149308 | 0.800962 | 0.499674 | 3.974558 | 0.213383 | 0.554797 | 0.231820 |
| attack_30_60 | attack_weight | 0.391516 | -0.359829 | 0.937207 | -0.141152 | 0.762628 | 0.479710 | 3.974558 | 0.256060 | 0.512120 | 0.231820 |
| attack_35_55 | attack_weight | 0.375023 | -0.345035 | 0.889531 | -0.133101 | 0.724615 | 0.459610 | 3.974558 | 0.298737 | 0.469443 | 0.231820 |
| attack_40_50_quality_pullback_35_55_idle_25qqq | combo | 0.365129 | -0.348450 | 0.870271 | -0.175166 | 0.744971 | 0.460481 | 2.789730 | 0.375610 | 0.428908 | 0.195482 |
| attack_40_50_idle_25qqq | combo | 0.360448 | -0.348450 | 0.870416 | -0.190380 | 0.736112 | 0.455271 | 2.870514 | 0.378030 | 0.426767 | 0.195203 |
| strong_trend_35_55_quality_pullback_35_55 | combo | 0.358502 | -0.324548 | 0.870235 | -0.101642 | 0.715341 | 0.430219 | 5.794884 | 0.330493 | 0.437302 | 0.232206 |
| attack_40_50 | attack_weight | 0.358332 | -0.330017 | 0.841809 | -0.125159 | 0.686938 | 0.439386 | 3.974558 | 0.341413 | 0.426767 | 0.231820 |
| strong_trend_30_60 | attack_weight | 0.355458 | -0.329251 | 0.909043 | -0.126988 | 0.722027 | 0.420171 | 6.947398 | 0.311049 | 0.457131 | 0.231820 |
| strong_trend_35_55_idle_25qqq | combo | 0.353026 | -0.343081 | 0.899576 | -0.189094 | 0.747261 | 0.435646 | 4.852407 | 0.372013 | 0.432784 | 0.195203 |
| pullback_quality_30_60 | pullback_quality | 0.351527 | -0.314930 | 0.793695 | -0.090777 | 0.674995 | 0.434348 | 3.958401 | 0.376831 | 0.390964 | 0.232206 |

## Top 2023+ CAGR Candidates
| strategy | theme | CAGR | Max Drawdown | 2020 Return | 2022 Return | 2023 Return | 2023+ CAGR | Turnover/Year | Average QQQ Weight | Average TQQQ Weight | Average Cash Weight |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| attack_25_65 | attack_weight | 0.407796 | -0.374397 | 0.984770 | -0.149308 | 0.800962 | 0.499674 | 3.974558 | 0.213383 | 0.554797 | 0.231820 |
| attack_30_60 | attack_weight | 0.391516 | -0.359829 | 0.937207 | -0.141152 | 0.762628 | 0.479710 | 3.974558 | 0.256060 | 0.512120 | 0.231820 |
| attack_40_50_quality_pullback_35_55_idle_25qqq | combo | 0.365129 | -0.348450 | 0.870271 | -0.175166 | 0.744971 | 0.460481 | 2.789730 | 0.375610 | 0.428908 | 0.195482 |
| attack_35_55 | attack_weight | 0.375023 | -0.345035 | 0.889531 | -0.133101 | 0.724615 | 0.459610 | 3.974558 | 0.298737 | 0.469443 | 0.231820 |
| attack_40_50_idle_25qqq | combo | 0.360448 | -0.348450 | 0.870416 | -0.190380 | 0.736112 | 0.455271 | 2.870514 | 0.378030 | 0.426767 | 0.195203 |
| idle_50qqq | idle_exposure | 0.343580 | -0.354790 | 0.842836 | -0.248163 | 0.746738 | 0.449425 | 1.987279 | 0.457323 | 0.384090 | 0.158587 |
| attack_40_50 | attack_weight | 0.358332 | -0.330017 | 0.841809 | -0.125159 | 0.686938 | 0.439386 | 3.974558 | 0.341413 | 0.426767 | 0.231820 |
| strong_trend_35_55_idle_25qqq | combo | 0.353026 | -0.343081 | 0.899576 | -0.189094 | 0.747261 | 0.435646 | 4.852407 | 0.372013 | 0.432784 | 0.195203 |
| idle_25qqq | idle_exposure | 0.343544 | -0.333675 | 0.821976 | -0.183135 | 0.697697 | 0.434713 | 2.870514 | 0.420707 | 0.384090 | 0.195203 |
| pullback_quality_30_60 | pullback_quality | 0.351527 | -0.314930 | 0.793695 | -0.090777 | 0.674995 | 0.434348 | 3.958401 | 0.376831 | 0.390964 | 0.232206 |

## Best By Theme
| strategy | theme | CAGR | Max Drawdown | 2020 Return | 2022 Return | 2023 Return | 2023+ CAGR | Turnover/Year | Average QQQ Weight | Average TQQQ Weight | Average Cash Weight |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| attack_25_65 | attack_weight | 0.407796 | -0.374397 | 0.984770 | -0.149308 | 0.800962 | 0.499674 | 3.974558 | 0.213383 | 0.554797 | 0.231820 |
| attack_40_50_quality_pullback_35_55_idle_25qqq | combo | 0.365129 | -0.348450 | 0.870271 | -0.175166 | 0.744971 | 0.460481 | 2.789730 | 0.375610 | 0.428908 | 0.195482 |
| pullback_quality_30_60 | pullback_quality | 0.351527 | -0.314930 | 0.793695 | -0.090777 | 0.674995 | 0.434348 | 3.958401 | 0.376831 | 0.390964 | 0.232206 |
| idle_50qqq_positive_ma20 | idle_exposure | 0.344403 | -0.314772 | 0.794111 | -0.117330 | 0.649612 | 0.419052 | 4.033799 | 0.388158 | 0.384090 | 0.227752 |

## Findings
- Baseline: 34.15% CAGR / -31.48% MaxDD / 41.91% 2023+ CAGR.
- Best CAGR candidate: `attack_25_65` at 40.78% CAGR / -37.44% MaxDD.
- Best candidate within 5pp worse MaxDD than baseline: `attack_30_60` at 39.15% CAGR / -35.98% MaxDD.
- Best 2023+ candidate: `attack_25_65` at 49.97% 2023+ CAGR / -37.44% MaxDD.
- The cleanest growth lever is shifting a moderate amount of QQQ into TQQQ during risk-on states. Idle QQQ exposure helps less, and stricter pullback quality gates mostly give up too much upside.

## Caveats
- Nasdaq close data is not adjusted for dividends, so absolute CAGR should be compared mainly within this study.
- This is a research-only experiment; no live allocation code was changed.
