"""Inventory rebalance warnings for cross-exchange arbitrage (no transfers: manual only)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True)
class RebalanceAdvice:
    exchange: str
    asset_low: str
    share_pct: Decimal
    message: str


def rebalance_advice(
    inventories: dict[str, dict[str, Decimal]],  # exchange -> asset -> value in common quote
    base: str,
    quote: str,
    min_share_pct: Decimal,
) -> list[RebalanceAdvice]:
    out: list[RebalanceAdvice] = []
    for ex, inv in inventories.items():
        b, q = inv.get(base, ZERO), inv.get(quote, ZERO)
        total = b + q
        if total <= 0:
            continue
        for asset, value in ((base, b), (quote, q)):
            share = value / total * 100
            if share < min_share_pct:
                richest = max(
                    (e for e in inventories if e != ex),
                    key=lambda e: inventories[e].get(asset, ZERO),
                    default=None,
                )
                hint = f" {richest} borsasından {asset} aktarın" if richest else ""
                out.append(
                    RebalanceAdvice(
                        ex,
                        asset,
                        share,
                        f"{ex}: {asset} payı %{share:.1f} (< %{min_share_pct}); arbitraj tek yöne "
                        f"kilitlenebilir.{hint} (manuel transfer; bot para çekmez).",
                    )
                )
    return out
