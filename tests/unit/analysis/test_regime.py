from __future__ import annotations

import pandas as pd

from bot.analysis.regime import Regime, detect_regime, regime_series
from tests.fixtures.loader import synthetic_1h


def share(s: pd.Series, value: Regime) -> float:
    return float((s == value.value).mean())


def test_fixture_segments_are_classified() -> None:
    r = regime_series(synthetic_1h())["regime"]
    assert share(r.iloc[300:400], Regime.TREND_UP) > 0.9
    assert share(r.iloc[650:800], Regime.RANGE) > 0.75
    assert share(r.iloc[1100:1200], Regime.TREND_DOWN) > 0.9
    assert share(r.iloc[1220:1350], Regime.VOLATILE) > 0.9


def test_warmup_is_unknown() -> None:
    r = regime_series(synthetic_1h())["regime"]
    assert (r.iloc[:150] == Regime.UNKNOWN.value).all()


def test_detect_regime_last_bar() -> None:
    df = synthetic_1h()
    st = detect_regime(df.iloc[:1150])
    assert st.regime is Regime.TREND_DOWN
    assert st.adx is not None and st.adx > 25
    assert st.ema_slope is not None and st.ema_slope < 0
    assert detect_regime(df.iloc[:0]).regime is Regime.UNKNOWN
    assert Regime.RANGE.label_tr == "yatay (range)"


def test_squeeze_flag_in_range() -> None:
    r = regime_series(synthetic_1h())
    assert r["squeeze"].iloc[400:800].any()
