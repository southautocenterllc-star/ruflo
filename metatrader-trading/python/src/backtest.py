"""Backtester for the EMA-crossover strategy (mirrors mql5/Experts/MACrossEA.mq5).

Runs anywhere (no MT5 needed) on OHLC data from a CSV, from MT5 history, or synthetic data.

Model:
- Signal on a closed bar's cross; entry at the NEXT bar's open (+/- spread);
  SL/TP are checked from the entry bar onward.
- Exits only via SL/TP, checked on high/low; if both hit in one bar, SL is assumed first.
- Position size = risk_pct of equity at the SL distance, like the EA.
- One position at a time.

Usage:
    python src/backtest.py --synthetic
    python src/backtest.py --csv EURUSD_H1.csv --spread 0.00015 --sl 0.0030 --tp 0.0060
    python src/backtest.py --mt5 EURUSD --bars 5000
CSV columns: time,open,high,low,close (header row required).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Params:
    fast: int = 10
    slow: int = 30
    sl: float = 0.0030        # stop distance in price units
    tp: float = 0.0060        # take-profit distance in price units
    spread: float = 0.00015   # paid on entry, in price units
    risk_pct: float = 1.0     # % of equity risked per trade
    start_equity: float = 10_000.0


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"]).sort_values("time")
    return df.reset_index(drop=True)


def load_mt5(symbol: str, bars: int = 5000) -> pd.DataFrame:
    import MetaTrader5 as mt5  # Windows + terminal only

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, bars)
    finally:
        mt5.shutdown()
    if rates is None:
        raise RuntimeError(f"No data for {symbol}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df[["time", "open", "high", "low", "close"]]


def synthetic(n: int = 5000, seed: int = 42) -> pd.DataFrame:
    """Random-walk OHLC. Useful for smoke tests only; it has no real edge."""
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0008, n))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.0004, n))
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="h"),
        "open": open_,
        "high": np.maximum(open_, close) + wick,
        "low": np.minimum(open_, close) - wick,
        "close": close,
    })


def signals(close: pd.Series, fast: int, slow: int) -> pd.Series:
    """+1 on a bullish cross, -1 on a bearish cross, else 0 (evaluated on closed bars)."""
    f = close.ewm(span=fast, adjust=False).mean()
    s = close.ewm(span=slow, adjust=False).mean()
    up = (f.shift(1) <= s.shift(1)) & (f > s)
    down = (f.shift(1) >= s.shift(1)) & (f < s)
    return up.astype(int) - down.astype(int)


def run(df: pd.DataFrame, p: Params = Params()) -> tuple[pd.DataFrame, pd.Series]:
    """Return (trades, equity curve indexed like df)."""
    if p.sl <= 0 or p.tp <= 0:
        raise ValueError("sl and tp must be positive")
    sig = signals(df["close"], p.fast, p.slow).to_numpy()
    o, h, l = df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    equity = p.start_equity
    curve = np.full(len(df), p.start_equity, dtype=float)
    trades = []
    pos = 0
    entry = sl = tp = 0.0
    entry_i = 0

    for i in range(1, len(df)):
        if not pos and sig[i - 1] != 0:
            pos = int(sig[i - 1])
            entry = o[i] + (p.spread if pos == 1 else -p.spread)
            sl = entry - pos * p.sl
            tp = entry + pos * p.tp
            entry_i = i
        if pos:
            exit_price = None
            if pos == 1:
                if l[i] <= sl:
                    exit_price = sl
                elif h[i] >= tp:
                    exit_price = tp
            else:
                if h[i] >= sl:
                    exit_price = sl
                elif l[i] <= tp:
                    exit_price = tp
            if exit_price is not None:
                pnl_price = (exit_price - entry) * pos
                r = pnl_price / p.sl                      # in units of risk
                equity += equity * p.risk_pct / 100 * r
                trades.append({"entry_i": entry_i, "exit_i": i, "side": pos,
                               "entry": entry, "exit": exit_price, "r": r,
                               "equity": equity})
                pos = 0
        curve[i] = equity

    return pd.DataFrame(trades), pd.Series(curve, index=df.index, name="equity")


def metrics(trades: pd.DataFrame, curve: pd.Series, p: Params = Params()) -> dict:
    if trades.empty:
        return {"trades": 0, "final_equity": float(curve.iloc[-1])}
    wins = trades.loc[trades["r"] > 0, "r"].sum()
    losses = -trades.loc[trades["r"] <= 0, "r"].sum()
    dd = (curve / curve.cummax() - 1).min()
    return {
        "trades": len(trades),
        "win_rate_%": round(100 * (trades["r"] > 0).mean(), 1),
        "profit_factor": round(wins / losses, 2) if losses > 0 else float("inf"),
        "avg_R": round(trades["r"].mean(), 3),
        "return_%": round(100 * (curve.iloc[-1] / p.start_equity - 1), 2),
        "max_drawdown_%": round(100 * dd, 2),
        "final_equity": round(float(curve.iloc[-1]), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--mt5", metavar="SYMBOL")
    src.add_argument("--synthetic", action="store_true")
    ap.add_argument("--bars", type=int, default=5000)
    d = Params()
    for name in ("fast", "slow"):
        ap.add_argument(f"--{name}", type=int, default=getattr(d, name))
    for name in ("sl", "tp", "spread", "risk_pct"):
        ap.add_argument(f"--{name.replace('_', '-')}", type=float, default=getattr(d, name))
    a = ap.parse_args()

    df = load_csv(a.csv) if a.csv else load_mt5(a.mt5, a.bars) if a.mt5 else synthetic(a.bars)
    p = Params(fast=a.fast, slow=a.slow, sl=a.sl, tp=a.tp, spread=a.spread, risk_pct=a.risk_pct)
    trades, curve = run(df, p)
    for k, v in metrics(trades, curve, p).items():
        print(f"{k:>16}: {v}")


if __name__ == "__main__":
    main()
