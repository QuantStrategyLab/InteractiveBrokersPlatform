# growth_pullback_systematic_v1 hypotheses

## Workspace scope
- This research stays entirely inside US equities.
- `crypto-linked equities` here means US-listed stocks with material crypto-linked business exposure.
- No spot / futures / on-chain crypto assets are used.
- No `CryptoLeaderRotation` logic is used.
- No `BinancePlatform` code or data path is used.

## Testable hypotheses

### H1. Best stock line may be broad growth / leadership, not technology-only
Research translation:
- Compare broad large-cap eligible / leadership subsets against tech-heavy subsets.
- If tech wins only in a narrow window, do not hard-code it as the default stock line.

### H2. Useful edge may be controlled pullback inside strong trends, not naive dip buying
Research translation:
- Separate moderate pullback + trend intact + recovery confirmation from falling knives.
- Run both `trend_only_control` and `naive_dip_buy_control` as explicit controls.

### H3. Price-first may be better than fake historical valuation overlays
Research translation:
- If the workspace lacks reliable point-in-time valuation/fundamental history, skip valuation in V1.
- Do not pretend current/retro valuation fields are point-in-time clean.

### H4. Crypto-linked equities may be a thematic bucket, not a primary stock universe
Research translation:
- Build a small, explicit, auditable crypto-linked equity list inside the US-equity universe.
- Test whether it has stable standalone alpha or is just a high-beta thematic bucket.

### H5. Human mistakes should become rule-design goals
Human weakness -> rule design target:
- 越跌越买 -> require trend intact and recovery confirmation before ranking a pullback highly.
- 对熟悉赛道过度集中 -> enforce holdings count, single-name cap, sector cap.
- 把好公司和好买点混为一谈 -> separate leader score from entry / pullback score.
- 没有统一退出或降暴露规则 -> keep benchmark + breadth regime and BOXX parking.

## V1 data-policy decision
- Valuation / fundamentals are skipped in V1.
- Reason: current workspace does not expose reliable point-in-time historical valuation/fundamental data.
- This is a deliberate data-quality decision, not a claim that valuation never matters.
