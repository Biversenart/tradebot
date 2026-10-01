"""Portfolio risk (spec §5.6): exposure, correlation, historical VaR/CVaR, risk-at-stop."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
import pandas as pd

from bot.core.models import PositionSide

ZERO = Decimal(0)


@dataclass(frozen=True)
class PositionView:
    symbol: str
    side: PositionSide
    qty: Decimal
    entry: Decimal
    stop: Decimal
    mark: Decimal
    strategy: str = ""

    @property
    def notional(self) -> Decimal:
        return self.qty * self.mark

    @property
    def signed_notional(self) -> Decimal:
        return self.notional if self.side is PositionSide.LONG else -self.notional

    @property
    def risk_at_stop(self) -> Decimal:
        move = self.mark - self.stop if self.side is PositionSide.LONG else self.stop - self.mark
        return max(ZERO, move) * self.qty


@dataclass
class PortfolioView:
    equity: Decimal
    positions: list[PositionView] = field(default_factory=list)
    balances: dict[str, Decimal] = field(default_factory=dict)  # asset -> value in quote

    def exposure(self, symbol: str | None = None) -> Decimal:
        return sum(
            (p.notional for p in self.positions if symbol is None or p.symbol == symbol), ZERO
        )

    @property
    def risk_at_stop(self) -> Decimal:
        return sum((p.risk_at_stop for p in self.positions), ZERO)


@dataclass(frozen=True)
class PortfolioRisk:
    equity: Decimal
    total_exposure: Decimal
    exposure_pct: Decimal
    risk_at_stop: Decimal
    risk_at_stop_pct: Decimal
    stablecoin_pct: Decimal | None
    var: float | None  # loss amount at confidence (positive)
    cvar: float | None
    correlation: pd.DataFrame | None


def returns_frame(closes: dict[str, pd.Series], lookback: int) -> pd.DataFrame:
    df = pd.DataFrame(closes).sort_index().iloc[-(lookback + 1) :]
    return df.pct_change().dropna(how="all")


def correlation_matrix(closes: dict[str, pd.Series], lookback: int) -> pd.DataFrame:
    return returns_frame(closes, lookback).corr()


def historical_var(
    positions: list[PositionView], closes: dict[str, pd.Series], lookback: int, confidence: float
) -> tuple[float | None, float | None]:
    """1-bar historical VaR/CVaR of the current book (positive numbers = losses)."""
    if not positions:
        return 0.0, 0.0
    rets = returns_frame(closes, lookback)
    pnl = pd.Series(0.0, index=rets.index)
    for p in positions:
        if p.symbol not in rets:
            return None, None
        pnl = pnl + float(p.signed_notional) * rets[p.symbol].fillna(0.0)
    if len(pnl) < 20:
        return None, None
    q = float(np.quantile(pnl.to_numpy(), 1 - confidence))
    tail = pnl[pnl <= q]
    return max(0.0, -q), max(0.0, -float(tail.mean())) if len(tail) else max(0.0, -q)


def portfolio_risk(
    view: PortfolioView,
    closes: dict[str, pd.Series] | None = None,
    lookback: int = 720,
    confidence: float = 0.95,
    stablecoins: tuple[str, ...] = ("USDT", "USDC"),
) -> PortfolioRisk:
    eq = view.equity
    total = view.exposure()
    stable = None
    if view.balances:
        tot_bal = sum(view.balances.values(), ZERO)
        if tot_bal > 0:
            stable = (
                sum((v for a, v in view.balances.items() if a in stablecoins), ZERO) / tot_bal * 100
            )
    var = cvar = None
    corr = None
    if closes:
        var, cvar = historical_var(view.positions, closes, lookback, confidence)
        corr = correlation_matrix(closes, lookback)
    ras = view.risk_at_stop
    return PortfolioRisk(
        equity=eq,
        total_exposure=total,
        exposure_pct=total / eq * 100 if eq > 0 else ZERO,
        risk_at_stop=ras,
        risk_at_stop_pct=ras / eq * 100 if eq > 0 else ZERO,
        stablecoin_pct=stable,
        var=var,
        cvar=cvar,
        correlation=corr,
    )
