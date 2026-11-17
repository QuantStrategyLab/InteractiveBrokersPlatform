English | [简体中文](ibkr-valuation-correction.zh-CN.md)

# IBKR Strategy Snapshot Valuation Boundaries

For common stocks (`STK`), strategy snapshots use `marketValue` from the cached IBKR `portfolio()` row paired strictly by `account + conId` against `positions()`, requiring matching quantities and currencies. Missing, non-finite, identity-mismatched, or cross-currency market values block the snapshot rather than substituting average cost.

`Position.average_cost` continues to come from `positions().avgCost`. Strategy equity is computed from the selected account's native cash and stock market values; native cash preserves the account-returned value, and cash-only execution buying power remains capped by that cash rather than broker margin buying power. USD `NetLiquidation` is recorded separately as broker NLV evidence along with any difference from strategy equity, without overwriting cash. `BASE` is never treated as USD. Stock snapshots process only `STK` rows in the target currency; options remain listed separately in metadata, and other derivatives are excluded from stock valuation.

Snapshots record the market-value source and read timestamp. Because cached `portfolio()` entries typically lack per-quote timestamps, the timestamp reflects the observation time only and does not prove underlying quote freshness. No market-data requests, FX conversions, or extra broker reads are performed.

Pinned dependency revisions are declared in `pyproject.toml` and `uv.lock`. The strategy-scope projection preserves `Position.average_cost`, `currency`, and `account_id` across round-trip conversion for unique symbols (including short positions), rejects duplicate symbols explicitly, and keeps missing cost unknown.

Regression coverage in `tests/test_strategy_runtime.py` exercises known/unknown cost, non-default currency and synthetic account identity retention, short positions, omission of explicit zero positions, and duplicate-symbol rejection using synthetic fixtures. Passing local tests and dependency pin checks verifies local projection behavior only and does not establish production deployment, multi-currency broker support, or live account recovery.
