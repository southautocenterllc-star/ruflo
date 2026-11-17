# metatrader-trading

Starter project for algorithmic trading with MetaTrader 5.

```
mql5/Experts/MACrossEA.mq5   EMA crossover Expert Advisor (risk-% position sizing, SL/TP)
python/src/bot.py            Python signal/order script (dry-run by default)
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

## Warning
Educational template, not financial advice and not a proven strategy.
Backtest, then use a **demo account** before risking real money.
