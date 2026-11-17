# metatrader-trading

Starter project for algorithmic trading with MetaTrader 5.

```
mql5/Experts/MACrossEA.mq5   EMA crossover Expert Advisor (risk-% position sizing, SL/TP)
python/src/bot.py            Python signal/order script (dry-run by default)
python/src/backtest.py       Backtester for the same EMA-cross strategy (CSV, MT5 history or synthetic)
python/tests/                pytest suite for the backtester
python/requirements.txt
```

## MQL5 Expert Advisor
1. In MT5: File → Open Data Folder → `MQL5/Experts`, copy `MACrossEA.mq5` there.
2. Open MetaEditor (F4), compile (F7).
3. Attach to a chart, or run it in the Strategy Tester first.

## Python
Requires Windows with the MT5 terminal installed and logged in.
```
cd python
pip install -r requirements.txt
python src/bot.py
```
Optional env vars: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`.
Set `DRY_RUN = False` in `bot.py` only after testing.

## Backtesting
Works on any OS; MT5 is only needed for `--mt5`.
```
cd python
python src/backtest.py --synthetic                     # smoke test on random data (no real edge)
python src/backtest.py --csv EURUSD_H1.csv --spread 0.00015 --sl 0.0030 --tp 0.0060
python src/backtest.py --mt5 EURUSD --bars 5000        # Windows + MT5 terminal
python src/backtest.py --oanda EUR_USD --bars 10000 --save-csv EURUSD_H1.csv   # OANDA data
python -m pytest tests
```
Risk controls (ideas taken from the `ruflo-neural-trader` skills: trader-risk, trader-regime, trader-backtest):
- Metrics: Sharpe, Sortino, VaR95/CVaR95 per trade.
- Circuit breakers: `--daily-limit 3 --weekly-limit 5` (% of equity; 0 disables) stop new entries after the limit.
- Regime filter: `--min-adx 20` trades only in trending markets.
- `--walk-forward 4` runs 4 consecutive windows to check the results hold across time
  (the strategy has no fitted parameters, so this measures stability, not optimisation).

OANDA data (read-only, no orders are sent): create a **demo** account and API token at OANDA, then
set `OANDA_TOKEN` (and optionally `OANDA_ENV=practice|live`, default `practice`) in your own shell.
Never paste the token into chat, code or git. Instruments use OANDA names (`EUR_USD`, `GBP_USD`, `XAU_USD`),
`--granularity M15|H1|H4|D`. `--save-csv` stores the data so you can re-run offline with `--csv`.

CSV needs a header row: `time,open,high,low,close`.
Model: entry at the next bar's open plus spread, SL/TP checked on high/low (SL first if both hit),
1 position at a time, position size = risk % of equity at the SL distance. No swaps/commission/slippage,
so treat results as optimistic. Keep `--sl/--tp/--spread` in price units (EURUSD: 0.0030 = 30 pips).

## Warning
Educational template, not financial advice and not a proven strategy.
Backtest, then use a **demo account** before risking real money.
