"""BaseStrategy (spec §5.4). The SAME classes run live and in backtests.

A strategy implements `compute(df)`, a *causal* function: row t may only use data up to and
including bar t (rolling/ewm windows, never `shift(-k)`). Live trading evaluates the last
row of the candle buffer; the backtest evaluates every row once and acts on bar t+1.
`assert_causal` (tests) verifies this property.

Strategies never send orders: they emit `Signal`s carrying a full `TradePlan`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict

from bot.analysis.plan import to_price
from bot.analysis.regime import Regime
from bot.config.schema import AnalysisConfig
from bot.core.models import Candle, Fill, Horizon, PositionSide, Signal, Ticker, TradePlan
from bot.core.timeframes import timeframe_delta

SIGNAL_COLUMNS = ("signal", "stop", "score", "atr", "reason")


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeframe: str = "1h"
    enabled: bool = True


class BaseStrategy(ABC):
    name: ClassVar[str]
    Params: ClassVar[type[StrategyParams]] = StrategyParams
    allowed_regimes: ClassVar[frozenset[Regime]] = frozenset(Regime)
    allow_short: ClassVar[bool] = True
    max_positions: ClassVar[int] = 1

    def __init__(
        self,
        symbol: str,
        exchange: str = "binance",
        params: Mapping[str, Any] | StrategyParams | None = None,
        analysis: AnalysisConfig | None = None,
        buffer_bars: int = 1500,
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        if isinstance(params, StrategyParams):
            self.params = params
        else:
            self.params = self.Params.model_validate(dict(params or {}))
        self.timeframe = self.params.timeframe
        self.analysis = analysis or AnalysisConfig()
        self.buffer_bars = buffer_bars
        self._rows: list[dict[str, Any]] = []
        self._index: list[pd.Timestamp] = []

    # ---------------------------------------------------------------- to implement
    @property
    @abstractmethod
    def warmup_bars(self) -> int: ...

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a frame indexed like `df` with SIGNAL_COLUMNS (signal in {-1,0,1})."""

    # ---------------------------------------------------------------- live hooks
    def on_candle(self, candle: Candle) -> None:
        if not candle.closed or candle.symbol != self.symbol or candle.timeframe != self.timeframe:
            return
        ts = pd.Timestamp(candle.open_time)
        if self._index and ts <= self._index[-1]:
            return  # duplicate / out of order
        self._index.append(ts)
        self._rows.append(
            {
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": float(candle.volume),
            }
        )
        if len(self._rows) > self.buffer_bars:
            del self._rows[0]
            del self._index[0]

    def on_ticker(self, ticker: Ticker) -> None:  # noqa: B027 - optional hook
        """Optional hook."""

    def on_fill(self, fill: Fill) -> None:  # noqa: B027 - optional hook
        """Optional hook."""

    def buffer(self) -> pd.DataFrame:
        return pd.DataFrame(self._rows, index=pd.DatetimeIndex(self._index, name="open_time"))

    def generate_signals(self, now: datetime | None = None) -> list[Signal]:
        if len(self._rows) < self.warmup_bars:
            return []
        df = self.buffer()
        frame = self.compute(df)
        row = frame.iloc[-1]
        sig = self.signal_from_row(df.index[-1], float(df["close"].iloc[-1]), row, now)
        return [sig] if sig is not None else []

    # ---------------------------------------------------------------- helpers
    def empty_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=df.index)
        out["signal"] = 0
        out["stop"] = float("nan")
        out["score"] = 0.0
        out["atr"] = float("nan")
        out["reason"] = ""
        return out

    def bar_close_time(self, ts: pd.Timestamp) -> datetime:
        t = ts.to_pydatetime()
        t = t if t.tzinfo else t.replace(tzinfo=UTC)
        return t + timeframe_delta(self.timeframe)

    def horizon(self) -> Horizon:
        tf = self.analysis.timeframes
        if self.timeframe in tf.short:
            return Horizon.SCALP
        if self.timeframe in tf.long:
            return Horizon.POSITION
        return Horizon.SWING

    def build_plan(
        self,
        ts: pd.Timestamp,
        close: float,
        direction: int,
        stop: float,
        atr: float,
        score: float,
        reason: str,
        now: datetime | None = None,
    ) -> TradePlan | None:
        """Entry band at the close, structural/ATR stop, R-multiple targets from config."""
        if direction == 0 or stop != stop or atr != atr or atr <= 0:
            return None
        long = direction > 0
        band = 0.1 * atr
        e_lo, e_hi = (close - band, close) if long else (close, close + band)
        mid = (e_lo + e_hi) / 2
        risk = abs(mid - stop)
        if risk <= 0 or (long and stop >= e_lo) or (not long and stop <= e_hi):
            return None
        sgn = 1 if long else -1
        tps = tuple(mid + sgn * float(r) * risk for r in self.analysis.trade_plan.take_profits)
        if min(tps) <= 0:
            return None
        try:
            return TradePlan(
                symbol=self.symbol,
                exchange=self.exchange,
                side=PositionSide.LONG if long else PositionSide.SHORT,
                horizon=self.horizon(),
                timeframe=self.timeframe,
                entry_low=to_price(e_lo),
                entry_high=to_price(e_hi),
                stop_loss=to_price(stop),
                take_profits=tuple(to_price(t) for t in tps),
                confluence_score=Decimal(str(round(max(0.0, min(100.0, score)), 2))),
                reasons=(f"{self.name}: {reason}",) if reason else (self.name,),
                invalidation=f"{self.timeframe} kapanışı stop ({to_price(stop)}) ötesinde",
                created_at=now or self.bar_close_time(ts),
            )
        except ValueError:
            return None

    def signal_from_row(
        self, ts: pd.Timestamp, close: float, row: pd.Series, now: datetime | None = None
    ) -> Signal | None:
        direction = int(row["signal"])
        if direction == 0 or (direction < 0 and not self.allow_short):
            return None
        plan = self.build_plan(
            ts,
            close,
            direction,
            float(row["stop"]),
            float(row["atr"]),
            float(row["score"]),
            str(row["reason"]),
            now,
        )
        if plan is None:
            return None
        return Signal(
            strategy=self.name,
            exchange=self.exchange,
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=plan.side,
            score=plan.confluence_score,
            timestamp=plan.created_at,
            plan=plan,
            reasons=plan.reasons,
        )


def assert_causal(strategy: BaseStrategy, df: pd.DataFrame, checkpoints: list[int]) -> None:
    """compute(df[:k]).iloc[-1] must equal compute(df).iloc[k-1] (no look-ahead)."""
    full = strategy.compute(df)
    for k in checkpoints:
        part = strategy.compute(df.iloc[:k]).iloc[-1]
        ref = full.iloc[k - 1]
        for col in ("signal", "stop", "score"):
            a, b = part[col], ref[col]
            same = (a != a and b != b) or abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b)))
            if not same:
                raise AssertionError(
                    f"{strategy.name}: look-ahead at bar {k} column {col}: {a} != {b}"
                )
