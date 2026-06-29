# InteractiveBrokersPlatform

IBKR execution platform for us_equity, hk_equity, and quant_combo strategies. Cloud Run with account-group config, SEHK/HKD support.

## Key Files

- `main.py` — Flask app with /run, /dry-run, /probe, /health, /monitor-dispatch
- `strategy_registry.py` — Imports US + HK + Combo catalogs, 3-way merge
- `runtime_config_support.py` — PlatformRuntimeSettings with IB gateway config
- `application/ibkr_portfolio.py` — Portfolio snapshot via IB API
- `application/ibkr_order_execution.py` — Order submission via IB API

## Deployment

- 4 account services: u15998061, u16608560, u18308207, u18336562
- u18336562: monthly DCA schedule (45 15 1-7 * *, America/New_York)
- All share same container image, different env vars
- Scheduler SAs per account

## HK Strategies

- HK market: Asia/Hong_Kong timezone, XHKG calendar
- Currently excluded: hk_dividend_gold_defensive_rotation, tech_communication_pullback_enhancement
