"""Backtester for the EMA-crossover strategy (mirrors mql5/Experts/MACrossEA.mq5).

Runs anywhere (no MT5 needed) on OHLC data from a CSV, MT5 history, OANDA, or synthetic data.

Model:
- Signal on a closed bar's cross; entry at the NEXT bar's open (+/- spread);
  SL/TP are checked from the entry bar onward.
- Exits only via SL/TP, checked on high/low; if both hit in one bar, SL is assumed first.
- Position size = risk_pct of equity at the SL distance, like the EA.
- One position at a time.
- Circuit breakers: no new entries after the daily/weekly loss limit is hit (realised equity).
- Optional regime filter: trade only when ADX >= --min-adx.

Usage:
    python src/backtest.py --synthetic
    python src/backtest.py --csv EURUSD_H1.csv --spread 0.00015 --sl 0.0030 --tp 0.0060
    python src/backtest.py --mt5 EURUSD --bars 5000
    python src/backtest.py --oanda EUR_USD --bars 10000 --save-csv EURUSD_H1.csv   # needs OANDA_TOKEN
CSV columns: time,open,high,low,close (header row required).
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
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
    daily_loss_limit: float = 3.0    # circuit breaker: stop opening trades after -3% in a day (0 = off)
    weekly_loss_limit: float = 5.0   # ... or -5% in a week (0 = off)
    min_adx: float = 0.0             # regime filter: only trade when ADX >= this (0 = off, ~20 = trending)
    bars_per_year: int = 24 * 252    # for annualising Sharpe/Sortino (H1 bars)


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


OANDA_HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}


def _oanda_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec - fixed OANDA hosts only
        return json.load(r)


def load_oanda(instrument: str = "EUR_USD", bars: int = 5000, granularity: str = "H1",
               fetch=_oanda_get) -> pd.DataFrame:
    """Download completed mid-price candles from OANDA v3 (read-only, no orders are sent).

    Needs env var OANDA_TOKEN (create it in your OANDA demo account). OANDA_ENV is
    'practice' (default) or 'live'. Instrument uses OANDA naming: EUR_USD, GBP_USD, XAU_USD...
    """
    token = os.getenv("OANDA_TOKEN")
    if not token:
        raise RuntimeError("Set OANDA_TOKEN (and optionally OANDA_ENV=practice|live)")
    env = os.getenv("OANDA_ENV", "practice")
    if env not in OANDA_HOSTS:
        raise RuntimeError("OANDA_ENV must be 'practice' or 'live'")
    base = f"{OANDA_HOSTS[env]}/v3/instruments/{urllib.parse.quote(instrument)}/candles"
    rows, to = [], None
    while len(rows) < bars:
        q = {"granularity": granularity, "price": "M", "count": min(5000, bars - len(rows))}
        if to:
            q["to"] = to
        candles = [c for c in fetch(f"{base}?{urllib.parse.urlencode(q)}", token)["candles"] if c["complete"]]
        if not candles:
            break
        rows = [{"time": c["time"], "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
                 "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"])} for c in candles] + rows
        to = candles[0]["time"]
        if len(candles) < q["count"]:
            break
    if not rows:
        raise RuntimeError(f"No candles returned for {instrument}")
    df = pd.DataFrame(rows).drop_duplicates("time")
    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(None)
    return df.sort_values("time").tail(bars).reset_index(drop=True)


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


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ADX. High (>~20-25) = trending regime, low = ranging."""
    up, down = df["high"].diff(), -df["low"].diff()
    plus = np.where((up > down) & (up > 0), up, 0.0)
    minus = np.where((down > up) & (down > 0), down, 0.0)
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    a = 1 / n
    atr = tr.ewm(alpha=a, adjust=False).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=a, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=a, adjust=False).mean().fillna(0)


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
    trend = adx(df).to_numpy() if p.min_adx > 0 else None
    if "time" in df:
        t = pd.to_datetime(df["time"])
        day_key = t.dt.floor("D").factorize()[0]
        week_key = t.dt.to_period("W").astype(str).factorize()[0]
    else:
        day_key = week_key = np.zeros(len(df), dtype=int)
    day_eq = week_eq = equity
    cur_day = cur_week = -1

    for i in range(1, len(df)):
        if day_key[i] != cur_day:
            cur_day, day_eq = day_key[i], equity
        if week_key[i] != cur_week:
            cur_week, week_eq = week_key[i], equity
        halted = (
            (p.daily_loss_limit > 0 and equity / day_eq - 1 <= -p.daily_loss_limit / 100)
            or (p.weekly_loss_limit > 0 and equity / week_eq - 1 <= -p.weekly_loss_limit / 100)
        )
        regime_ok = trend is None or trend[i - 1] >= p.min_adx
        if not pos and sig[i - 1] != 0 and not halted and regime_ok:
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
    rets = curve.pct_change().dropna()
    ann = np.sqrt(p.bars_per_year)
    sharpe = rets.mean() / rets.std() * ann if rets.std() > 0 else 0.0
    downside = np.sqrt((np.minimum(rets, 0) ** 2).mean())   # downside deviation over all bars
    sortino = rets.mean() / downside * ann if downside > 0 else 0.0
    pct = trades["r"] * p.risk_pct                      # per-trade return, % of equity
    var95 = -np.percentile(pct, 5)
    cvar95 = -pct[pct <= np.percentile(pct, 5)].mean()
    return {
        "trades": len(trades),
        "win_rate_%": round(100 * (trades["r"] > 0).mean(), 1),
        "profit_factor": round(wins / losses, 2) if losses > 0 else float("inf"),
        "avg_R": round(trades["r"].mean(), 3),
        "return_%": round(100 * (curve.iloc[-1] / p.start_equity - 1), 2),
        "max_drawdown_%": round(100 * dd, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "VaR95_trade_%": round(var95, 2),
        "CVaR95_trade_%": round(cvar95, 2),
        "final_equity": round(float(curve.iloc[-1]), 2),
    }


def walk_forward(df: pd.DataFrame, p: Params = Params(), splits: int = 4) -> pd.DataFrame:
    """Run the strategy on `splits` consecutive windows to check stability across time.

    The strategy has no fitted parameters, so this measures consistency (does the edge hold in
    every period?), not out-of-sample optimisation. If you tune parameters, tune on early
    windows only and judge on later ones.
    """
    if splits < 2:
        raise ValueError("splits must be >= 2")
    size = len(df) // splits
    rows = []
    for k in range(splits):
        w = df.iloc[k * size:(k + 1) * size if k < splits - 1 else len(df)].reset_index(drop=True)
        tr, cv = run(w, p)
        rows.append({"window": k + 1, **metrics(tr, cv, p)})
    return pd.DataFrame(rows).set_index("window")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv")
    src.add_argument("--mt5", metavar="SYMBOL")
    src.add_argument("--oanda", metavar="INSTRUMENT", help="e.g. EUR_USD (needs OANDA_TOKEN)")
    src.add_argument("--synthetic", action="store_true")
    ap.add_argument("--granularity", default="H1", help="OANDA candle size: M15, H1, H4, D")
    ap.add_argument("--save-csv", metavar="PATH", help="save the downloaded data for offline reuse")
    ap.add_argument("--bars", type=int, default=5000)
    d = Params()
    for name in ("fast", "slow"):
        ap.add_argument(f"--{name}", type=int, default=getattr(d, name))
    for name in ("sl", "tp", "spread", "risk_pct"):
        ap.add_argument(f"--{name.replace('_', '-')}", type=float, default=getattr(d, name))
    ap.add_argument("--min-adx", type=float, default=d.min_adx, help="regime filter, e.g. 20")
    ap.add_argument("--daily-limit", type=float, default=d.daily_loss_limit)
    ap.add_argument("--weekly-limit", type=float, default=d.weekly_loss_limit)
    ap.add_argument("--walk-forward", type=int, default=0, metavar="N", help="also run N consecutive windows")
    a = ap.parse_args()

    if a.csv:
        df = load_csv(a.csv)
    elif a.mt5:
        df = load_mt5(a.mt5, a.bars)
    elif a.oanda:
        df = load_oanda(a.oanda, a.bars, a.granularity)
    else:
        df = synthetic(a.bars)
    if a.save_csv:
        df.to_csv(a.save_csv, index=False)
        print(f"saved {len(df)} bars to {a.save_csv}")
    p = Params(fast=a.fast, slow=a.slow, sl=a.sl, tp=a.tp, spread=a.spread, risk_pct=a.risk_pct,
               min_adx=a.min_adx, daily_loss_limit=a.daily_limit, weekly_loss_limit=a.weekly_limit)
    trades, curve = run(df, p)
    for k, v in metrics(trades, curve, p).items():
        print(f"{k:>16}: {v}")
    if a.walk_forward:
        print("\nwalk-forward windows:")
        print(walk_forward(df, p, a.walk_forward).to_string())


if __name__ == "__main__":
    main()
