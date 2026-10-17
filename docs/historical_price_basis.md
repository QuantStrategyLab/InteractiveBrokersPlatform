# Historical signal price basis

`PRICE_BASIS` is an optional, non-secret environment configuration key. The
equivalent strategy runtime JSON key is `price_basis`. Precedence is explicit
`PRICE_BASIS`, runtime JSON, strategy manifest default, then `split_adjusted`.
Environment values are validated before configuration clients are resolved;
the merged runtime value is validated before strategy evaluation. Only the four
exact, case-sensitive values below are accepted (no aliases or empty values).

| Requested `price_basis` | yfinance (`auto_adjust=False`) | IBKR historical bars |
| --- | --- | --- |
| `split_adjusted` (default) | Native Open/High/Low/Close | TRADES only |
| `split_dividend_adjusted` | Adj Close, with O/H/L scaled per row | ADJUSTED_LAST only |
| `raw_unadjusted` | Unsupported: Close already adjusts for splits | Unsupported: TRADES already adjusts for splits |
| `total_return_index` | Rejected for these OHLC/history consumers | Rejected for these OHLC/history consumers |

The default preserves #602's successful Close/OHLC/Volume output. Synthetic
golden fixtures freeze IEEE-754 output from main commit
`8e173b2bd1cb903f00ffbfe77e7f5f709aa00b7a`, including both MultiIndex orientations.
The intentional behavior change is **fail closed**: unavailable or unprovable
requested data now raises `PriceBasisError` rather than switching basis, returning
empty history, dropping invalid rows or inventing missing OHLC/volume. A missing
Close never selects Adj Close. Provider errors/empty responses also stop the
request, rather than implicitly switching providers. No fallback is retained as
a safety requirement.

For dividend adjustment, each row uses `factor = Adj Close / Close`. Multiply
that row's Open, High and Low by the factor; use Adj Close directly for Close.
All four prices then share `split_dividend_adjusted`. This is the vendor's
dividend-aware research series, not an audited reinvested wealth index or an
executable price. `return_basis` and dividend cash accounting remain separate.
Do not book dividends twice when using this series for return research.

Volume is declared as `volume_basis=provider_native_unscaled`: retain the vendor
value and units exactly, with no adapter split/dividend rescaling. Native zero
volume remains zero; missing, nonfinite or negative volume is unavailable and
fails. This label does not certify physical corporate-action normalization or
cross-vendor volume equivalence.

The IBKR proxy selects exactly one endpoint while consuming the existing pinned
QPK loader. For split-only, it skips QPK's initial ADJUSTED_LAST probe and permits
TRADES. For split+dividend, ADJUSTED_LAST is passed to IBKR; an empty/error response
is remembered and its typed failure is re-raised if QPK attempts TRADES. Native
OHLC fields are checked before QPK's legacy conversion can synthesize missing
values. This policy belongs to this platform's signal adapter; shared QPK callers
and dependency pins are unchanged.

`HistoricalCandles` carries batch metadata without changing candle dictionaries
or numeric values. Custom injected loaders must accept `price_basis`, return
this batch with each OHLC field's declared basis and preserve volume. Plain,
unlabelled or mixed-basis results fail. yfinance's known native column contract
is checked against optional `frame.attrs['field_price_basis']` declarations;
invalid OHLC ranges and ambiguous multi-symbol columns also fail. Range checks
alone cannot infer an undocumented vendor adjustment method.

Success provenance (`qsl.signal_data_provenance.v2`) and logs include requested
basis, resolved basis, provider, source columns and volume basis. Typed failures
include the same fields, a null resolved basis and a bounded reason code. They
never log provider exception text, prices, account data or credentials. Legacy
recorder input without recognized metadata is labelled `unknown_legacy` for
inventory only; it is never an accepted request value or a loader success.

This config controls historical loaders. External indicators and feature
snapshots retain their separate contracts; setting it does not rebuild or
certify their upstream inputs. It does not approve any deploy or replace a
cloud-only, frozen paired strategy comparison.

Related proposals: [QPK #678](https://github.com/QuantStrategyLab/QuantPlatformKit/pull/678)
and [UESP #542](https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/pull/542).
Needs owner review before IBKR deploy.
