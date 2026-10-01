"""Arbitrage execution with leg-risk handling (spec §5.5). v1: PAPER MODE ONLY.

Cross-exchange: both legs are sent concurrently as IOC-style limit orders at the worst
book price the scanner used (caps slippage); unfilled remainders are cancelled. If the filled
quantities differ, the imbalance is hedged at market:
    bought more than sold -> sell the excess on the buy exchange (unwind)
    sold more than bought -> buy back the excess on the sell exchange (restore inventory)
Triangular: legs run sequentially (each leg funds the next). If a leg fails, whatever is held
is converted back to the start asset at market through the markets already used (unwind).
Every leg goes through RiskManager (assess_intent for new risk, assess_exit for hedges, which
stay allowed under the kill switch). Live/testnet execution is refused in v1.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from bot.arbitrage.models import ArbKind, Leg, Opportunity
from bot.config import Mode
from bot.core.models import Order, OrderIntent, OrderRequest, OrderStatus, OrderType, Side
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import ExchangeAdapterError
from bot.log import get_logger
from bot.risk.manager import ApprovedIntent, MarketContext, RiskManager
from bot.risk.portfolio import PortfolioView

ZERO = Decimal(0)
_log = get_logger(__name__)


@dataclass
class ArbExecution:
    opportunity: Opportunity
    status: str = "pending"  # completed | hedged | aborted | rejected
    orders: list[Order] = field(default_factory=list)
    hedges: list[Order] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


class LiveExecutionNotAllowedError(RuntimeError):
    pass


class ArbitrageExecutor:
    def __init__(
        self,
        adapters: dict[str, ExchangeAdapter],
        risk: RiskManager,
        mode: Mode,
        portfolio: Callable[[], PortfolioView],
        enabled: bool = True,
    ) -> None:
        if mode is not Mode.PAPER:
            raise LiveExecutionNotAllowedError(
                "Arbitraj yürütmesi v1'de yalnızca paper modda; diğer modlarda yalnızca loglanır."
            )
        self.adapters = adapters
        self.risk = risk
        self.portfolio = portfolio
        self.enabled = enabled
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- helpers
    def _intent(
        self,
        leg: Leg,
        order_type: OrderType,
        amount: Decimal | None = None,
        reduce_only: bool = False,
        price: Decimal | None = None,
    ) -> OrderIntent:
        return OrderIntent(
            exchange=leg.exchange,
            symbol=leg.symbol,
            side=leg.side,
            order_type=order_type,
            amount=amount or leg.amount,
            price=price,
            reduce_only=reduce_only,
            strategy="arbitrage",
        )

    async def _send(self, approved: ApprovedIntent, ioc: bool) -> Order:
        i = approved.intent
        ad = self.adapters[i.exchange]
        req = OrderRequest(
            client_order_id=f"tbarb{uuid.uuid4().hex[:24]}",
            exchange=i.exchange,
            symbol=i.symbol,
            side=i.side,
            order_type=i.order_type,
            amount=i.amount,
            price=i.price,
            intent_id=i.intent_id,
        )
        order = await ad.create_order(req)
        if ioc and not order.status.is_terminal:
            try:
                order = await ad.cancel_order(
                    order.exchange_order_id or order.client_order_id, i.symbol
                )
            except ExchangeAdapterError as exc:
                _log.warning("arb_cancel_failed", error=str(exc))
        return order

    async def _hedge(
        self, ex: ArbExecution, exchange: str, symbol: str, side: Side, qty: Decimal, reason: str
    ) -> None:
        if qty <= 0:
            return
        if side is Side.BUY:
            # buy-back is funded by the sell proceeds, which are net of fees: cap to what we hold
            qty = await self._affordable(exchange, symbol, qty)
        intent = OrderIntent(
            exchange=exchange,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            amount=qty,
            reduce_only=True,
            strategy="arbitrage_hedge",
            reason=reason,
        )
        d = self.risk.assess_exit(intent, qty)
        if d.approved is None:
            ex.reasons.append(f"hedge reddedildi: {d.report.reasons}")
            return
        try:
            order = await self._send(d.approved, ioc=False)
        except ExchangeAdapterError as exc:
            ex.reasons.append(f"hedge başarısız ({exchange} {symbol}): {exc}")
            return
        ex.hedges.append(order)
        ex.reasons.append(f"hedge: {side.value} {order.filled} {symbol}@{exchange} ({reason})")

    async def _affordable(self, exchange: str, symbol: str, qty: Decimal) -> Decimal:
        ad = self.adapters[exchange]
        try:
            m = ad.market_info(symbol)
            bal = await ad.fetch_balance()
            book = await ad.fetch_order_book(symbol, 20)
        except ExchangeAdapterError:
            return qty
        quote = bal.get(m.quote)
        ask = book.best_ask
        if quote is None or ask is None:
            return qty
        max_qty = quote.free / (ask.price * (1 + m.taker_fee))
        qty = min(qty, max_qty)
        return (qty / m.precision.lot_size).to_integral_value(
            rounding="ROUND_DOWN"
        ) * m.precision.lot_size

    # ---------------------------------------------------------------- entry point
    async def execute(self, opp: Opportunity) -> ArbExecution:
        ex = ArbExecution(opp)
        if not self.enabled:
            ex.status = "rejected"
            ex.reasons.append("yürütme kapalı (yalnızca log)")
            return ex
        async with self._lock:
            if opp.kind is ArbKind.CROSS:
                await self._cross(ex)
            else:
                await self._triangular(ex)
        _log.info("arbitrage_executed", route=opp.route, status=ex.status, reasons=ex.reasons)
        return ex

    async def _cross(self, ex: ArbExecution) -> None:
        buy, sell = ex.opportunity.legs
        pv = self.portfolio()
        approvals = []
        for leg in (buy, sell):
            d = self.risk.assess_intent(
                self._intent(leg, OrderType.LIMIT, price=leg.worst_price), pv, MarketContext()
            )
            if d.approved is None:
                ex.status = "rejected"
                ex.reasons += d.report.reasons
                return
            approvals.append(d.approved)
        results = await asyncio.gather(
            *(self._send(a, ioc=True) for a in approvals), return_exceptions=True
        )
        filled: list[Decimal] = []
        for leg, res in zip((buy, sell), results, strict=True):
            if isinstance(res, BaseException):
                ex.reasons.append(f"{leg.exchange} bacağı hata: {res}")
                filled.append(ZERO)
            else:
                ex.orders.append(res)
                filled.append(res.filled if res.status is not OrderStatus.REJECTED else ZERO)
        f_buy, f_sell = filled
        if f_buy == 0 and f_sell == 0:
            ex.status = "aborted"
            return
        diff = f_buy - f_sell
        if diff == 0:
            ex.status = "completed"
            return
        if diff > 0:  # holding extra base on the buy venue -> unwind
            await self._hedge(ex, buy.exchange, buy.symbol, Side.SELL, diff, "fazla alış")
        else:  # sold base we did not buy -> buy it back on the sell venue
            await self._hedge(ex, sell.exchange, sell.symbol, Side.BUY, -diff, "eksik alış")
        ex.status = "hedged"

    async def _triangular(self, ex: ArbExecution) -> None:
        legs = ex.opportunity.legs
        pv = self.portfolio()
        first = self._intent(legs[0], OrderType.LIMIT, price=legs[0].worst_price)
        d = self.risk.assess_intent(first, pv, MarketContext())
        if d.approved is None:
            ex.status = "rejected"
            ex.reasons += d.report.reasons
            return
        done: list[tuple[Leg, Order]] = []
        approved: ApprovedIntent | None = d.approved
        for k, leg in enumerate(legs):
            if approved is None:
                # later legs convert inventory acquired by this cycle (risk-reducing)
                amount = self._next_amount(done, leg)
                exit_intent = self._intent(leg, OrderType.MARKET, amount=amount, reduce_only=True)
                dk = self.risk.assess_exit(exit_intent, exit_intent.amount)
                approved = dk.approved
            if approved is None:
                ex.reasons.append(f"bacak {k + 1} onaylanmadı")
                break
            try:
                order = await self._send(approved, ioc=True)
            except ExchangeAdapterError as exc:
                ex.reasons.append(f"bacak {k + 1} hata: {exc}")
                break
            ex.orders.append(order)
            if order.filled <= 0:
                ex.reasons.append(f"bacak {k + 1} dolmadı")
                break
            done.append((leg, order))
            approved = None
        if len(done) == len(legs):
            ex.status = "completed"
            return
        if not done:
            ex.status = "aborted"
            return
        # unwind: walk back the completed legs in reverse
        for leg, order in reversed(done):
            back = Side.SELL if leg.side is Side.BUY else Side.BUY
            await self._hedge(ex, leg.exchange, leg.symbol, back, order.filled, "üçgen geri alma")
        ex.status = "hedged"

    def _fee_and_lot(self, leg: Leg) -> tuple[Decimal, Decimal | None]:
        try:
            m = self.adapters[leg.exchange].market_info(leg.symbol)
        except (ExchangeAdapterError, KeyError):
            return Decimal("0.001"), None
        return m.taker_fee, m.precision.lot_size

    def _next_amount(self, done: list[tuple[Leg, Order]], leg: Leg) -> Decimal:
        """Base amount for the next leg from what the previous fill actually produced.

        Paper/most spot venues charge the fee in quote: proceeds of a sell are net of fee, and a
        buy needs price x qty x (1 + fee) of quote.
        """
        if not done:
            return leg.amount
        prev_leg, prev = done[-1]
        prev_fee, _ = self._fee_and_lot(prev_leg)
        if prev_leg.side is Side.BUY:
            got = prev.filled  # base received
        else:
            got = prev.filled * (prev.average_price or ZERO) * (1 - prev_fee)  # quote received
        fee, lot = self._fee_and_lot(leg)
        if leg.side is Side.BUY:  # spend `got` quote at the worst price plus fee
            qty = got / (leg.worst_price * (1 + fee)) if leg.worst_price > 0 else ZERO
        else:
            qty = got
        qty = min(leg.amount, qty) if leg.side is Side.BUY else qty
        if lot is not None and lot > 0:
            qty = (qty / lot).to_integral_value(rounding="ROUND_DOWN") * lot
        return qty
