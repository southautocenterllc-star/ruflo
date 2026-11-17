"""MetaTrader 5 Python starter: connect, fetch candles, MA-crossover signal.

The MetaTrader5 package only works on Windows with the MT5 terminal installed.
Defaults to dry-run; set DRY_RUN = False only on a DEMO account after testing.
"""
import os
import sys

import MetaTrader5 as mt5
import pandas as pd

SYMBOL = "EURUSD"
TIMEFRAME = mt5.TIMEFRAME_H1
FAST, SLOW = 10, 30
VOLUME = 0.01
DRY_RUN = True


def connect() -> None:
    # Credentials come from the environment, never hardcode them.
    login = os.getenv("MT5_LOGIN")
    kwargs = {}
    if login:
        kwargs = dict(
            login=int(login),
            password=os.environ["MT5_PASSWORD"],
            server=os.environ["MT5_SERVER"],
        )
    if not mt5.initialize(**kwargs):
        sys.exit(f"MT5 initialize failed: {mt5.last_error()}")


def signal(symbol: str) -> str:
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, SLOW * 3)
    if rates is None:
        sys.exit(f"No data for {symbol}: {mt5.last_error()}")
    close = pd.DataFrame(rates)["close"]
    fast, slow = close.ewm(span=FAST).mean(), close.ewm(span=SLOW).mean()
    # use last two CLOSED bars (-3 -> -2); -1 is the forming bar
    if fast.iloc[-3] <= slow.iloc[-3] and fast.iloc[-2] > slow.iloc[-2]:
        return "buy"
    if fast.iloc[-3] >= slow.iloc[-3] and fast.iloc[-2] < slow.iloc[-2]:
        return "sell"
    return "hold"


def send_order(symbol: str, side: str) -> None:
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if side == "buy" else tick.bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": VOLUME,
        "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "deviation": 20,
        "magic": 20260101,
        "comment": "python-bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    print(mt5.order_send(request))


def main() -> None:
    connect()
    try:
        side = signal(SYMBOL)
        print(f"{SYMBOL} signal: {side}")
        if side != "hold" and not DRY_RUN:
            send_order(SYMBOL, side)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
