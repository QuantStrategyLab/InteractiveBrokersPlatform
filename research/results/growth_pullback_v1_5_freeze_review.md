# growth_pullback_systematic_v1.5 freeze review and branch packaging

## Canonical branch default
| role | status | strategy | branch_name | name | previous_candidate_name | family | universe | normalization | min_adv20_usd | sector_whitelist | symbol_whitelist | notes | score_template | holdings_count | single_name_cap | sector_cap | hold_bonus | regime | benchmark_symbol | breadth_mode | breadth_symbols | breadth_thresholds | exposures | residual_proxy | cost_assumption_bps_one_way | branch_role | canonicalized_from |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cash_buffer_branch_default | research_only | growth_pullback_systematic_v1 | cash_buffer_branch | cash_buffer_branch_default | cash_buffer_a__hb10__base__adv50 | tech_heavy_pullback | tech_heavy | universe_cross_sectional | 50000000.000000 | ["Information Technology", "Communication"] | [] | Sector proxy only: Information Technology + Communication; no sub-industry data available | balanced_pullback | 8 | 0.100000 | 0.400000 | 0.100000 | qqq_breadth | QQQ | broad | [] | {"hard": 0.35, "soft": 0.55} | {"hard_defense": 0.0, "risk_on": 0.8, "soft_defense": 0.6} | simple_excess_return_vs_QQQ | 5.000000 | cash-buffered parallel branch | growth_pullback_systematic_v1.4 cash_buffer_refinement |

## Final role table
| strategy | role | full_cagr | oos_cagr | oos_cagr_minus_qqq | oos_max_drawdown | return_2022 | annual_turnover | avg_names_held | risk_on_realized_stock_weight | beta_vs_qqq | alpha_ann_vs_qqq | information_ratio_vs_qqq |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cash_buffer_branch_default | cash-buffered parallel branch | 0.234811 | 0.340680 | 0.238732 | -0.191763 | -0.120121 | 2.646607 | 5.898990 | 0.800000 | 0.502781 | 0.256476 | 0.874353 |
| qqq_plus_current_default | main offensive default | 0.241651 | 0.334010 | 0.232062 | -0.323779 | -0.015312 | 3.440286 | 9.465767 | 0.960000 | 0.744797 | 0.241211 | 0.836495 |
| coherent_full_deployment_branch | full-deployment tech pullback reference | 0.244121 | 0.315371 | 0.213423 | -0.210619 | -0.159632 | 3.642799 | 7.373737 | 1.000000 | 0.576097 | 0.234898 | 0.777800 |
| russell_1000_multi_factor_defensive | defensive base | 0.163311 | 0.184469 | 0.082521 | -0.254393 | -0.139659 | 3.926458 | 20.644166 | 1.000000 | 0.568919 | 0.122988 | 0.344425 |
| QQQ | benchmark reference | 0.179222 | 0.101948 | 0.000000 | -0.348280 | -0.325770 | 0.000000 | 1.000000 | 1.000000 | 1.000000 | 0.000000 |  |

## Consistency checks
| check | passed | detail |
| --- | --- | --- |
| geometry_capacity_matches_target | True | theoretical_capacity=0.8000, risk_on_target=0.8000 |
| risk_on_realized_matches_target | True | realized=0.8000, target=0.8000 |
| soft_defense_realized_matches_target | True | realized=0.6000, target=0.6000 |
| hard_defense_realized_zero | True | realized=0.0000, target=0.0000 |
| config_name_matches_canonical_name | True | config_name=cash_buffer_branch_default, canonical_name=cash_buffer_branch_default |
| previous_candidate_mapping_preserved | True | previous_candidate_name=cash_buffer_a__hb10__base__adv50 |
| canonical_metrics_match_previous_candidate | True | canonical_oos=0.340680, prev_oos=0.340680; canonical_2022=-0.120121, prev_2022=-0.120121 |
| recommendation_matches_branch_default_role | True | recommendation=cash_buffer_branch_default |
| manifest_role_matches_default_branch | True | manifest_role=cash-buffered parallel branch |

## Freeze review
- recommendation=cash_buffer_branch_default
- previous_candidate_name=cash_buffer_a__hb10__base__adv50
- branch_name=cash_buffer_branch_default
- role_vs_qqq_plus_current_default=并行分支
- keep_parallel_branch=True
- reason=canonical naming and branch role are now clear, but avg names / 2022 profile / recent micro-adjustment mean it should stop at default, not frozen
- frozen_blockers=average_names_held_still_low, 2022_still_not_clean_enough, just_canonicalized_from_small_hold_bonus_micro_adjustment

## Manifest summary
| branch_name | role | previous_candidate_name | benchmark | intended_use |
| --- | --- | --- | --- | --- |
| cash_buffer_branch_default | cash-buffered parallel branch | cash_buffer_a__hb10__base__adv50 | QQQ | Research-only parallel stock branch for concentrated tech leaders bought on controlled pullbacks with an explicit 20% cash/BOXX buffer in risk-on. |