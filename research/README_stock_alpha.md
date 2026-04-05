# Stock alpha research suite

这个研究脚本做三件事：

1. 复用 `russell_1000_multi_factor_defensive` 的现有真实数据代理回测结果。
2. 公平比较它和两个高弹性 ETF 策略：
   - `hybrid_growth_income`
   - `semiconductor_rotation_income`
3. 研究一个新的 price-only 进攻型个股候选：`qqq_plus_stock_alpha_v1`。

## 代码位置

- 研究脚本：`research/backtest_stock_alpha_suite.py`
- 结果目录：`research/results/`
- defensive 基线代码：`../UsEquityStrategies/src/us_equity_strategies/strategies/russell_1000_multi_factor_defensive.py`

## 怎么跑

默认会自动发现最新的 `official_monthly_v2_alias` Russell 数据 run：

```bash
PYTHONPATH=/Users/lisiyi/Projects/UsEquityStrategies/src:/Users/lisiyi/Projects/QuantPlatformKit/src \
/Users/lisiyi/Projects/InteractiveBrokersPlatform/.venv/bin/python \
research/backtest_stock_alpha_suite.py
```

也可以显式指定数据目录和结果目录：

```bash
PYTHONPATH=/Users/lisiyi/Projects/UsEquityStrategies/src:/Users/lisiyi/Projects/QuantPlatformKit/src \
/Users/lisiyi/Projects/InteractiveBrokersPlatform/.venv/bin/python \
research/backtest_stock_alpha_suite.py \
  --data-run-dir /Users/lisiyi/Projects/_local_runs/r1000_multifactor_defensive_20260403_official_monthly_v2_alias \
  --results-dir /Users/lisiyi/Projects/InteractiveBrokersPlatform/research/results
```

## 输出文件

- `stock_strategy_comparison.csv`
- `stock_strategy_comparison.md`
- `stock_strategy_ablations.csv`
- `stock_strategy_equity_curves.csv`
- `stock_strategy_rolling_36m_alpha_vs_qqq.csv`
- `stock_strategy_workspace_mapping.json`

## 当前数据假设

### Defensive / offensive 个股侧

- universe：IWB 历史持仓代理，不是 FTSE Russell 官方 PIT 成分
- price：Yahoo / yfinance
- ticker 修复：沿用 `official_monthly_v2_alias` 的 identifier-based alias
- offensive 的 `resid_mom_6_1` / `resid_mom_12_1` 在 V1 里是 **相对 QQQ 的简单超额收益 proxy**，不是严格回归残差动量
- group normalization 当前使用 **sector 内 z-score**

### ETF 策略侧

- full strategy 按当前默认逻辑回测
- normalized comparison 通过把账户规模放在 income layer 阈值下方，或者把阈值抬高，来关闭收入层
- `SPYI` / `QQQI` / `BOXX` 的早期缺失历史，在收益矩阵里按 **上市前 0% 日收益** 处理，相当于近似现金腿；结果里需要把这点当 caveat 看

## 研究重点

- full strategy vs normalized strategy 两层比较
- full sample / 2018-2021 / 2022 / 2023+
- rolling 36m alpha vs QQQ
- offensive V1 最少一轮 ablation：
  - universe
  - 持仓数 / 单票上限 / 行业上限
  - regime
  - 暴露档位

## 不在本次 V1 的内容

- 基本面质量因子
- earnings revision / PEAD
- 机器学习 / LLM 交易信号
- 付费数据源接入

这些都留到后续 V2 研究。
