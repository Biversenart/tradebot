from __future__ import annotations

from decimal import Decimal as D

import pandas as pd

from bot.backtest.bias import CheckStatus, bias_checks, gap_ratio
from bot.config.schema import BacktestConfig


def frame(n: int = 100, drop: int = 0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"close": range(n)}, index=idx)
    return df.drop(df.index[10 : 10 + drop]) if drop else df


def test_all_pass_with_realistic_costs_and_delisted_data() -> None:
    cfg = BacktestConfig(delisted_symbols=("LUNA/USDT",))
    checks = bias_checks(cfg, True, ["BTC/USDT", "LUNA/USDT"], {"BTC/USDT": frame()})
    assert all(c.status is CheckStatus.PASS for c in checks.checks)
    assert checks.lines()[0] == "Önyargı kontrolleri: TÜMÜ GEÇTİ"
    assert "LUNA/USDT" in checks.get("Survivorship").detail


def test_lookahead_and_unrealistic_costs_fail() -> None:
    checks = bias_checks(BacktestConfig(commission_pct=D(0), slippage_bps=D(0)), False, ["X"])
    assert checks.get("Lookahead (geleceği görme) testi").status is CheckStatus.FAIL
    assert checks.get("Gerçekçi maliyet").status is CheckStatus.FAIL
    assert not checks.passed and "BAŞARISIZ" in checks.lines()[0]


def test_survivorship_and_gaps_warn() -> None:
    assert gap_ratio(frame(), "1h") == 0.0
    assert abs(gap_ratio(frame(drop=5), "1h") - 0.05) < 1e-9
    checks = bias_checks(BacktestConfig(), True, ["BTC/USDT"], {"BTC/USDT": frame(drop=5)})
    assert checks.get("Survivorship").status is CheckStatus.WARN
    assert checks.get("Veri bütünlüğü").status is CheckStatus.WARN
    assert checks.passed and "uyarılarla" in checks.lines()[0]
