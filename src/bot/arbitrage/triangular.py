"""Triangular arbitrage on one exchange (spec §5.5.2), e.g. USDT -> BTC -> ETH -> USDT.

For every cycle start -> X -> Y -> start over listed markets, the start amount is pushed
through the three order books (walking depth, paying taker fee each leg). The executable
size is searched over a geometric ladder up to `max_notional_quote`; the best net result
above `min_net_profit_pct` is reported. Stale books (> max_book_age_ms) are skipped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bot.arbitrage.depth import sell_proceeds, spend_quote
from bot.arbitrage.models import ArbKind, Leg, Opportunity
from bot.config.schema import TriangularConfig
from bot.core.models import OrderBook, Side

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Market:
    symbol: str
    base: str
    quote: str
    book: OrderBook
    taker_fee: Decimal


def _convert(m: Market, have: str, amount: Decimal) -> tuple[Decimal, Leg] | None:
    """Convert `amount` of `have` through market m (buy base with quote, or sell base)."""
    fee = 1 - m.taker_fee
    if have == m.quote:  # buy base
        base, spent, worst = spend_quote(m.book.asks, amount)
        if spent < amount or base <= 0:
            return None
        return base * fee, Leg("", m.symbol, Side.BUY, base, spent / base, worst)
    if have == m.base:  # sell base
        got, proceeds, worst = sell_proceeds(m.book.bids, amount)
        if got < amount or got <= 0:
            return None
        return proceeds * fee, Leg("", m.symbol, Side.SELL, amount, proceeds / amount, worst)
    return None


def find_cycles(markets: Mapping[str, Market], start: str) -> list[tuple[Market, Market, Market]]:
    by_asset: dict[str, list[Market]] = {}
    for m in markets.values():
        by_asset.setdefault(m.base, []).append(m)
        by_asset.setdefault(m.quote, []).append(m)

    def other(m: Market, a: str) -> str:
        return m.quote if a == m.base else m.base

    cycles = []
    for m1 in by_asset.get(start, []):
        x = other(m1, start)
        for m2 in by_asset.get(x, []):
            if m2 is m1:
                continue
            y = other(m2, x)
            if y == start:
                continue
            for m3 in by_asset.get(y, []):
                if m3 in (m1, m2) or other(m3, y) != start:
                    continue
                cycles.append((m1, m2, m3))
    return cycles


def simulate(
    cycle: tuple[Market, Market, Market], start: str, amount: Decimal
) -> tuple[Decimal, list[Leg]] | None:
    have, qty = start, amount
    legs: list[Leg] = []
    for m in cycle:
        res = _convert(m, have, qty)
        if res is None:
            return None
        qty, leg = res
        legs.append(leg)
        have = m.base if have == m.quote else m.quote
    return qty, legs


def scan_triangular(
    markets: Mapping[str, Market], cfg: TriangularConfig, now: datetime, exchange: str
) -> list[Opportunity]:
    fresh = {
        k: m
        for k, m in markets.items()
        if (now - m.book.timestamp).total_seconds() * 1000 <= cfg.max_book_age_ms
    }
    out: list[Opportunity] = []
    threshold = cfg.min_net_profit_pct / HUNDRED
    ladder = [cfg.max_notional_quote / Decimal(2**k) for k in range(8)][::-1]
    for cycle in find_cycles(fresh, cfg.start_asset):
        best: tuple[Decimal, Decimal, list[Leg]] | None = None
        depth_limited = False
        for amount in ladder:
            res = simulate(cycle, cfg.start_asset, amount)
            if res is None:
                depth_limited = True
                break
            end, legs = res
            net = (end - amount) / amount
            if best is None or net * amount > best[1] * best[0]:
                best = (amount, net, legs)
        if best is None or best[1] < threshold:
            continue
        amount, net, legs = best
        fee_total = sum((m.taker_fee for m in cycle), ZERO)
        route = " -> ".join(m.symbol for m in cycle)
        out.append(
            Opportunity(
                kind=ArbKind.TRIANGULAR,
                route=f"{route}@{exchange}",
                legs=tuple(
                    Leg(exchange, lg.symbol, lg.side, lg.amount, lg.avg_price, lg.worst_price)
                    for lg in legs
                ),
                gross_pct=(net + fee_total) * HUNDRED,
                net_pct=net * HUNDRED,
                notional_quote=amount,
                expected_profit_quote=amount * net,
                depth_limited=depth_limited,
                latency_ms=max((now - m.book.timestamp).total_seconds() * 1000 for m in cycle),
                detected_at=now,
            )
        )
    return sorted(out, key=lambda o: o.net_pct, reverse=True)
