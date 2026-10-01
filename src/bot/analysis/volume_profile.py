"""Volume profile (POC / VAH / VAL). Each candle's volume is spread uniformly over its range."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class VolumeProfile:
    edges: np.ndarray
    volumes: np.ndarray
    poc: float
    vah: float
    val: float

    @property
    def total(self) -> float:
        return float(self.volumes.sum())


def volume_profile(df: pd.DataFrame, bins: int = 50, value_area_pct: float = 70.0) -> VolumeProfile:
    if df.empty:
        raise ValueError("Boş veri")
    lo_all, hi_all = float(df["low"].min()), float(df["high"].max())
    if hi_all <= lo_all:
        hi_all = lo_all + 1e-9
    edges = np.linspace(lo_all, hi_all, bins + 1)
    vols = np.zeros(bins)
    for low, high, vol in zip(df["low"], df["high"], df["volume"], strict=True):
        low, high, vol = float(low), float(high), float(vol)
        if vol <= 0:
            continue
        if high <= low:  # zero-range candle: all volume into its bin
            b = min(int(np.searchsorted(edges, low, side="right")) - 1, bins - 1)
            vols[max(b, 0)] += vol
            continue
        overlap = np.clip(np.minimum(edges[1:], high) - np.maximum(edges[:-1], low), 0, None)
        vols += vol * overlap / (high - low)
    poc_bin = int(np.argmax(vols))
    target = vols.sum() * value_area_pct / 100
    lo_b = hi_b = poc_bin
    acc = vols[poc_bin]
    while acc < target and (lo_b > 0 or hi_b < bins - 1):
        below = vols[lo_b - 1] if lo_b > 0 else -1.0
        above = vols[hi_b + 1] if hi_b < bins - 1 else -1.0
        if above >= below:
            hi_b += 1
            acc += above
        else:
            lo_b -= 1
            acc += below
    poc = float((edges[poc_bin] + edges[poc_bin + 1]) / 2)
    return VolumeProfile(edges, vols, poc, float(edges[hi_b + 1]), float(edges[lo_b]))
