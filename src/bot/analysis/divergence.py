"""RSI / MACD divergences between consecutive swing points (spec §5.3.e)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

import pandas as pd

from bot.analysis.structure import SwingKind, SwingPoint
from bot.config.schema import DivergenceParams


class DivergenceKind(StrEnum):
    REGULAR_BULLISH = "regular_bullish"
    REGULAR_BEARISH = "regular_bearish"
    HIDDEN_BULLISH = "hidden_bullish"
    HIDDEN_BEARISH = "hidden_bearish"

    @property
    def is_bullish(self) -> bool:
        return self in (DivergenceKind.REGULAR_BULLISH, DivergenceKind.HIDDEN_BULLISH)

    @property
    def label_tr(self) -> str:
        return {
            "regular_bullish": "pozitif uyumsuzluk",
            "regular_bearish": "negatif uyumsuzluk",
            "hidden_bullish": "gizli pozitif uyumsuzluk",
            "hidden_bearish": "gizli negatif uyumsuzluk",
        }[self.value]


@dataclass(frozen=True)
class Divergence:
    kind: DivergenceKind
    indicator: str
    start_index: int
    end_index: int
    price_start: float
    price_end: float
    ind_start: float
    ind_end: float


def find_divergences(
    swings: list[SwingPoint],
    indicator: pd.Series,
    name: str,
    params: DivergenceParams | None = None,
) -> list[Divergence]:
    p = params or DivergenceParams()
    values = indicator.to_numpy(dtype=float)
    out: list[Divergence] = []
    for kind in (SwingKind.LOW, SwingKind.HIGH):
        pts = [s for s in swings if s.kind is kind]
        for a, b in pairwise(pts):
            gap = b.index - a.index
            if not p.min_bars_between <= gap <= p.max_bars_between:
                continue
            ia, ib = values[a.index], values[b.index]
            if ia != ia or ib != ib:  # NaN
                continue
            dk: DivergenceKind | None = None
            if kind is SwingKind.LOW:
                if b.price < a.price and ib > ia:
                    dk = DivergenceKind.REGULAR_BULLISH
                elif b.price > a.price and ib < ia:
                    dk = DivergenceKind.HIDDEN_BULLISH
            else:
                if b.price > a.price and ib < ia:
                    dk = DivergenceKind.REGULAR_BEARISH
                elif b.price < a.price and ib > ia:
                    dk = DivergenceKind.HIDDEN_BEARISH
            if dk is not None:
                out.append(
                    Divergence(dk, name, a.index, b.index, a.price, b.price, float(ia), float(ib))
                )
    return sorted(out, key=lambda d: d.end_index)
