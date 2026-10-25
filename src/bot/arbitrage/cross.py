"""Cross-exchange arbitrage scanner (spec §5.5.1).

For each pair and exchange combination (buy on A at its asks, sell on B at its bids):
    net = (sell_avg_B - buy_avg_A) / buy_avg_A - taker_fee_A - taker_fee_B - slippage_buffer
The executable size is the largest quantity (in steps over the merged book levels) whose
*marginal* level still clears the threshold, capped by `max_notional_quote` and by inventory
(quote on A, base on B - no transfers). Books older than `max_leg_latency_ms` or whose
timestamps differ by more than that are rejected (stale prices are not arbitrage).
Prices on exchanges quoted in another currency (TRY bridge) are converted with an FX book.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bot.arbitrage.depth import buy_cost, sell_proceeds
from bot.arbitrage.models import ArbKind, Leg, Opportunity
from bot.config.schema import CrossExchangeConfig
from bot.core.models import OrderBook, OrderBookLevel, Side

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Venue:
    exchange: str
    symbol: str  # symbol on that exchange (e.g. BTC/USDT or BTC/TRY)
    book: OrderBook
    taker_fee: Decimal
    fx: Decimal = Decimal(1)  # multiply venue prices by this to get the common quote
    quote_balance: Decimal | None = None  # in venue quote currency
    base_balance: Decimal | None = None


def _converted(levels: tuple[OrderBookLevel, ...], fx: Decimal) -> tuple[OrderBookLevel, ...]:
    if fx == 1:
        return levels
    return tuple(OrderBookLevel(price=lvl.price * fx, amount=lvl.amount) for lvl in levels)


def _age_ms(book: OrderBook, now: datetime) -> float:
    return (now - book.timestamp).total_seconds() * 1000


def evaluate_pair(
    buy: Venue, sell: Venue, cfg: CrossExchangeConfig, now: datetime
) -> Opportunity | None:
    asks = _converted(buy.book.asks, buy.fx)
    bids = _converted(sell.book.bids, sell.fx)
    if not asks or not bids:
        return None
    latency = max(
        _age_ms(buy.book, now),
        _age_ms(sell.book, now),
        abs((buy.book.timestamp - sell.book.timestamp).total_seconds() * 1000),
    )
    if latency > cfg.max_leg_latency_ms:
        return None
    fees = buy.taker_fee + sell.taker_fee + cfg.slippage_buffer_pct / HUNDRED
    threshold = cfg.min_net_spread_pct / HUNDRED
    # top-of-book gross check first (cheap)
    if (bids[0].price - asks[0].price) / asks[0].price - fees < threshold:
        return None
    # walk: candidate sizes at every level boundary of either side
    cuts: set[Decimal] = set()
    acc = ZERO
    for lvl in asks:
        acc += lvl.amount
        cuts.add(acc)
    acc = ZERO
    for lvl in bids:
        acc += lvl.amount
        cuts.add(acc)
    cap_qty = cfg.max_notional_quote / asks[0].price
    if buy.quote_balance is not None:
        cap_qty = min(cap_qty, buy.quote_balance * buy.fx / asks[0].price)
    if sell.base_balance is not None:
        cap_qty = min(cap_qty, sell.base_balance)
    best: tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None = None
    depth_limited = False
    for qty in [*sorted(c for c in cuts if c > 0), cap_qty]:
        q = min(qty, cap_qty)
        if q <= 0:
            continue
        got_b, cost, worst_b = buy_cost(asks, q)
        got_s, proceeds, worst_s = sell_proceeds(bids, q)
        q_eff = min(got_b, got_s)
        if q_eff <= 0 or q_eff < q:
            depth_limited = True
            break
        buy_avg, sell_avg = cost / q_eff, proceeds / q_eff
        # marginal check: the deepest levels used must still clear the threshold
        if (worst_s - worst_b) / worst_b - fees < threshold:
            depth_limited = True
            break
        best = (q_eff, buy_avg, sell_avg, worst_b, worst_s)
        if q >= cap_qty:
            break
    if best is None:
        return None
    q, buy_avg, sell_avg, worst_b, worst_s = best
    gross = (sell_avg - buy_avg) / buy_avg
    net = gross - fees
    notional = q * buy_avg
    legs = (
        Leg(buy.exchange, buy.symbol, Side.BUY, q, buy_avg / buy.fx, worst_b / buy.fx),
        Leg(sell.exchange, sell.symbol, Side.SELL, q, sell_avg / sell.fx, worst_s / sell.fx),
    )
    return Opportunity(
        kind=ArbKind.CROSS,
        route=f"{buy.symbol}@{buy.exchange} -> {sell.symbol}@{sell.exchange}",
        legs=legs,
        gross_pct=gross * HUNDRED,
        net_pct=net * HUNDRED,
        notional_quote=notional,
        expected_profit_quote=notional * net,
        depth_limited=depth_limited,
        latency_ms=latency,
        detected_at=now,
    )


def scan_cross(
    venues: Mapping[str, Venue], cfg: CrossExchangeConfig, now: datetime
) -> list[Opportunity]:
    """All profitable (buy A, sell B) combinations for one pair across exchanges."""
    out: list[Opportunity] = []
    names = list(venues)
    for a in names:
        for b in names:
            if a == b:
                continue
            opp = evaluate_pair(venues[a], venues[b], cfg, now)
            if opp is not None:
                out.append(opp)
    return sorted(out, key=lambda o: o.net_pct, reverse=True)


def fx_mid(book: OrderBook) -> Decimal | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    return (book.best_bid.price + book.best_ask.price) / 2
