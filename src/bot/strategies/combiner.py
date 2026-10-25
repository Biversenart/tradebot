"""Weighted-vote signal combiner (spec §5.4). Causal because every member is causal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
from pydantic import Field

from bot.config.schema import AnalysisConfig
from bot.strategies.base import BaseStrategy, StrategyParams


class CombinerParams(StrategyParams):
    weights: dict[str, float] = Field(default_factory=dict)
    threshold: float = Field(default=0.5, gt=0, le=1)


class SignalCombiner(BaseStrategy):
    name = "signal_combiner"
    Params = CombinerParams
    params: CombinerParams

    def __init__(
        self,
        members: list[BaseStrategy],
        symbol: str,
        exchange: str = "binance",
        params: Mapping[str, Any] | None = None,
        analysis: AnalysisConfig | None = None,
    ) -> None:
        if not members:
            raise ValueError("Birleştirici en az bir strateji ister.")
        super().__init__(symbol, exchange, params, analysis)
        self.members = members
        self.timeframe = members[0].timeframe

    @property
    def warmup_bars(self) -> int:
        return max(m.warmup_bars for m in self.members)

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self.empty_frame(df)
        frames = {m.name: m.compute(df) for m in self.members}
        weights = {n: float(self.params.weights.get(n, 1.0)) for n in frames}
        total_w = sum(weights.values()) or 1.0
        vote = pd.Series(0.0, index=df.index)
        for n, f in frames.items():
            vote = vote + weights[n] * f["signal"].astype(float) * f["score"] / 100
        vote = vote / total_w
        sign = pd.Series(np.sign(vote.to_numpy()), index=df.index)
        direction = sign.where(vote.abs() >= self.params.threshold, 0).astype(int)
        out["signal"] = direction
        best_stop = pd.Series(np.nan, index=df.index)
        best_w = pd.Series(-1.0, index=df.index)
        reasons = pd.Series("", index=df.index)
        for n, f in frames.items():
            agree = (f["signal"] == direction) & (direction != 0)
            better = agree & (weights[n] > best_w)
            best_stop = best_stop.where(~better, f["stop"])
            best_w = best_w.where(~better, weights[n])
            reasons = reasons.where(~agree, reasons + n + " ")
        out["stop"] = best_stop
        out["score"] = (vote.abs() * 100).clip(upper=100)
        out["atr"] = next(iter(frames.values()))["atr"]
        out["reason"] = ("oy birliği: " + reasons.str.strip()).where(direction != 0, "")
        return out
