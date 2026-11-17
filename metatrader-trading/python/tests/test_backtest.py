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


def test_circuit_breaker_blocks_entries_after_daily_loss(monkeypatch):
    # two losing longs on the same day; second must be blocked by a 1% daily limit
    rows = [(10, 10, 10, 10)] * 2 + [(10, 10, 8, 9), (10, 10, 10, 10), (10, 10, 8, 9)]
    df = bars(rows)
    monkeypatch.setattr(bt, "signals", lambda *a, **k: pd.Series([0, 1, 0, 1, 0]))
    p = bt.Params(sl=1.0, tp=5.0, spread=0.0, risk_pct=2.0, daily_loss_limit=1.0, weekly_loss_limit=0)
    trades, _ = bt.run(df, p)
    assert len(trades) == 1
    p2 = bt.Params(sl=1.0, tp=5.0, spread=0.0, risk_pct=2.0, daily_loss_limit=0, weekly_loss_limit=0)
    assert len(bt.run(df, p2)[0]) == 2


def test_adx_filter_reduces_trades():
    df = bt.synthetic(3000)
    base = bt.run(df, bt.Params(daily_loss_limit=0, weekly_loss_limit=0))[0]
    filt = bt.run(df, bt.Params(min_adx=40, daily_loss_limit=0, weekly_loss_limit=0))[0]
    assert len(filt) <= len(base)


def test_walk_forward_and_risk_metrics():
    df = bt.synthetic(2000)
    wf = bt.walk_forward(df, bt.Params(), 4)
    assert len(wf) == 4
    t, c = bt.run(df)
    m = bt.metrics(t, c)
    assert {"sharpe", "sortino", "VaR95_trade_%", "CVaR95_trade_%"} <= m.keys()
    assert m["CVaR95_trade_%"] >= m["VaR95_trade_%"]


def _fake_oanda(total):
    """Fake OANDA endpoint honouring count + 'to' like the real API (newest-first paging)."""
    import urllib.parse as up
    times = pd.date_range("2024-01-01", periods=total, freq="h").strftime("%Y-%m-%dT%H:00:00.000000000Z")

    def fetch(url, token):
        assert token == "tok" and "/EUR_USD/candles" in url
        q = dict(up.parse_qsl(up.urlparse(url).query))
        end = list(times).index(q["to"]) if "to" in q else total
        sel = times[max(0, end - int(q["count"])):end]
        cs = [{"time": t, "complete": True, "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"}} for t in sel]
        cs.append({"time": "9999", "complete": False, "mid": {"o": "0", "h": "0", "l": "0", "c": "0"}}) if "to" not in q else None
        return {"candles": cs}
    return fetch


def test_load_oanda_pages_and_drops_incomplete(monkeypatch):
    monkeypatch.setenv("OANDA_TOKEN", "tok")
    df = bt.load_oanda("EUR_USD", bars=7000, fetch=_fake_oanda(9000))
    assert len(df) == 7000 and df["time"].is_monotonic_increasing and df["time"].is_unique
    assert df["close"].eq(1.15).all()


def test_load_oanda_requires_token(monkeypatch):
    monkeypatch.delenv("OANDA_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        bt.load_oanda("EUR_USD")
