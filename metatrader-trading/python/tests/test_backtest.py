import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import backtest as bt  # noqa: E402


def bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"]).assign(
        time=lambda d: pd.date_range("2024-01-01", periods=len(d), freq="h"))


def test_signals_detect_cross():
    close = pd.Series([1, 1, 1, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0], dtype=float)
    s = bt.signals(close, 2, 4)
    assert (s == 1).any() and (s == -1).any()


def test_take_profit_hit_gives_positive_R(monkeypatch):
    p = bt.Params(sl=1.0, tp=2.0, spread=0.0, risk_pct=1.0)
    df = bars([(10, 10, 10, 10)] * 3 + [(10, 10.5, 9.5, 10), (10, 13, 9.5, 12)])
    sig = pd.Series([0, 0, 1, 0, 0])               # long signal on bar 2
    monkeypatch.setattr(bt, "signals", lambda *a, **k: sig)
    trades, curve = bt.run(df, p)
    assert len(trades) == 1 and trades.loc[0, "r"] == pytest.approx(2.0)
    assert curve.iloc[-1] == pytest.approx(10_000 * 1.02)


def test_sl_wins_when_both_hit_same_bar(monkeypatch):
    p = bt.Params(sl=1.0, tp=1.0, spread=0.0)
    df = bars([(10, 10, 10, 10)] * 3 + [(10, 12, 8, 10)])
    monkeypatch.setattr(bt, "signals", lambda *a, **k: pd.Series([0, 0, 1, 0]))
    trades, _ = bt.run(df, p)
    assert trades.loc[0, "r"] == pytest.approx(-1.0)


def test_synthetic_runs_and_reports():
    trades, curve = bt.run(bt.synthetic(2000))
    m = bt.metrics(trades, curve)
    assert m["trades"] > 0 and m["max_drawdown_%"] <= 0


def test_invalid_params():
    with pytest.raises(ValueError):
        bt.run(bt.synthetic(100), bt.Params(sl=0))
