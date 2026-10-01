from __future__ import annotations

import numpy as np
import pytest

from bot.analysis.volume_profile import volume_profile
from tests.fixtures.loader import frame


def test_poc_and_value_area() -> None:
    # 10 bars trading 100-101 with volume 10, one bar 110-111 with volume 1
    o = [100.5] * 10 + [110.5]
    df = frame(o, [101.0] * 10 + [111.0], [100.0] * 10 + [110.0], o, [10.0] * 10 + [1.0])
    vp = volume_profile(df, bins=11, value_area_pct=70)
    assert vp.total == pytest.approx(101.0)
    assert vp.poc == pytest.approx(100.5)
    assert vp.val == pytest.approx(100.0)
    assert vp.vah == pytest.approx(101.0)


def test_volume_is_conserved_and_split_by_overlap() -> None:
    df = frame([1.0], [3.0], [1.0], [2.0], [8.0])
    vp = volume_profile(df, bins=4, value_area_pct=50)
    assert np.allclose(vp.volumes, [2, 2, 2, 2])
    assert vp.total == pytest.approx(8.0)


def test_empty_rejected() -> None:
    with pytest.raises(ValueError):
        volume_profile(frame([], [], [], []))
