"""Order execution (spec §5.7) and exchange-side protection (CLAUDE.md rule 9).

- Accepts only `ApprovedIntent` (RiskManager output, rule 4).
- Idempotent client order ids derived from the intent id: a retry after a timeout first looks
  the order up by client id, so a network blip never doubles an order.
- Entry -> wait for fill (partial fills tracked; remainder cancelled at timeout) -> immediately
  place a STOP order on the exchange for the filled size. If the stop cannot be placed the
  position is closed at market right away and an alert is raised.
- Exits (TP / trailing / time / kill switch) cancel the exchange stop, reduce at market and
  re-place the stop for the remainder.
- Every adapter error feeds the kill switch error counter; successes reset it.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from bot.core.clock import Clock, SystemClock
from bot.core.events import AlertLevel, RiskAlert
from bot.core.models import (
    MarketInfo,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
)
from bot.core.precision import round_amount, round_price
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import (
    ExchangeAdapterError,
    ExchangeNetworkError,
    ExchangeUnavailableError,
    OrderNotFoundError,
    RateLimitError,
)
from bot.log import get_logger
from bot.risk.kill_switch import KillSwitch
from bot.risk.manager import ApprovedIntent
from bot.storage.models import PositionRow
from bot.storage.repository import Repository

AlertSink = Callable[[RiskAlert], Awaitable[None]]
ZERO = Decimal(0)
RETRYABLE = (ExchangeNetworkError, ExchangeUnavailableError, RateLimitError)
_log = get_logger(__name__)


@dataclass(frozen=True)
class ExecutionConfig:
    order_timeout_seconds: float = 60.0
    poll_interval_seconds: float = 2.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    client_prefix: str = "tb"


def client_order_id(intent_id: str, role: str, n: int = 0, prefix: str = "tb") -> str:
    digest = hashlib.sha1(f"{intent_id}:{role}:{n}".encode(), usedforsecurity=False).hexdigest()
    return f"{prefix}{digest[:30]}"


class ExecutionEngine:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        repo: Repository,
        kill_switch: KillSwitch,
        config: ExecutionConfig | None = None,
        clock: Clock | None = None,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self.adapter = adapter
        self.repo = repo
        self.kill_switch = kill_switch
        self.cfg = config or ExecutionConfig()
        self.clock = clock or SystemClock()
        self.alert_sink = alert_sink

    # ---------------------------------------------------------------- plumbing
    async def alert(self, level: AlertLevel, code: str, message: str, **details: object) -> None:
        _log.warning("execution_alert", code=code, message=message)
        if self.alert_sink is not None:
            await self.alert_sink(
                RiskAlert(level=level, code=code, message=message, details=dict(details))
            )

    def market(self, symbol: str) -> MarketInfo | None:
        try:
            return self.adapter.market_info(symbol)
        except ExchangeAdapterError:
            return None

    def _cid(self, base: str, role: str, n: int = 0) -> str:
        return client_order_id(base, role, n, self.cfg.client_prefix)

    async def submit(
        self, request: OrderRequest, role: str, position_id: str | None = None, strategy: str = ""
    ) -> Order:
        """create_order with retries; idempotent through the client order id."""
        last: ExchangeAdapterError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                order = await self.adapter.create_order(request)
                self.kill_switch.record_success()
                await self.repo.save_order(order, role, position_id, strategy)
                return order
            except RETRYABLE as exc:
                last = exc
                await self.kill_switch.record_error(exc.kind, str(exc)[:120])
                await asyncio.sleep(self.cfg.retry_backoff_seconds * (2**attempt))
                try:  # did the order reach the exchange before the error?
                    order = await self.adapter.fetch_order_by_client_id(
                        request.client_order_id, request.symbol
                    )
                    await self.repo.save_order(order, role, position_id, strategy)
                    return order
                except ExchangeAdapterError:
                    continue
            except ExchangeAdapterError as exc:
                await self.kill_switch.record_error(exc.kind, str(exc)[:120])
                raise
        assert last is not None  # noqa: S101
        raise last

    async def refresh(self, order: Order) -> Order:
        try:
            if order.exchange_order_id:
                o = await self.adapter.fetch_order(order.exchange_order_id, order.symbol)
            else:
                o = await self.adapter.fetch_order_by_client_id(order.client_order_id, order.symbol)
        except OrderNotFoundError:
            o = await self.adapter.fetch_order_by_client_id(order.client_order_id, order.symbol)
        return o.model_copy(
            update={"client_order_id": order.client_order_id, "intent_id": order.intent_id}
        )

    async def await_fill(self, order: Order, role: str, position_id: str | None = None) -> Order:
        """Poll until terminal; at timeout cancel the remainder (partial fill is kept)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.order_timeout_seconds
        while not order.status.is_terminal:
            if loop.time() >= deadline:
                try:
                    await self.adapter.cancel_order(
                        order.exchange_order_id or order.client_order_id, order.symbol
                    )
                except ExchangeAdapterError as exc:
                    _log.warning("cancel_failed", error=str(exc))
                order = await self.refresh(order)
                break
            await asyncio.sleep(self.cfg.poll_interval_seconds)
            order = await self.refresh(order)
        await self.repo.save_order(order, role, position_id)
        return order

    # ---------------------------------------------------------------- entries
    async def open_position(
        self,
        approved: ApprovedIntent,
        take_profits: list[Decimal],
        timeframe: str = "1h",
    ) -> PositionRow | None:
        intent = approved.intent
        if intent.reduce_only:
            raise ValueError("open_position yalnızca giriş emirleri içindir.")
        if intent.stop_loss is None:
            raise ValueError("Stop-loss olmadan pozisyon açılamaz (kural 9).")
        info = self.market(intent.symbol)
        amount = round_amount(intent.amount, info.precision) if info else intent.amount
        price = intent.price
        if price is not None and info is not None:
            price = round_price(price, info.precision, intent.side)
        request = OrderRequest(
            client_order_id=self._cid(intent.intent_id, "entry"),
            exchange=intent.exchange,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            amount=amount,
            price=price,
            intent_id=intent.intent_id,
        )
        position_id = intent.intent_id
        order = await self.submit(request, "entry", position_id, intent.strategy)
        order = await self.await_fill(order, "entry", position_id)
        if order.filled <= 0:
            await self.alert(
                AlertLevel.INFO,
                "entry_not_filled",
                f"{intent.symbol} giriş emri dolmadı ({order.status}).",
            )
            return None
        entry_px = order.average_price or order.price or price or ZERO
        long = intent.side is Side.BUY
        pos = PositionRow(
            position_id=position_id,
            exchange=intent.exchange,
            symbol=intent.symbol,
            side=(PositionSide.LONG if long else PositionSide.SHORT).value,
            strategy=intent.strategy,
            timeframe=timeframe,
            amount=order.filled,
            initial_amount=order.filled,
            entry_price=entry_px,
            initial_stop=intent.stop_loss,
            stop_loss=intent.stop_loss,
            take_profits=[str(t) for t in take_profits],
            targets_hit=0,
            best_price=entry_px,
            realized_pnl=ZERO,
            fees=self._fee(order.filled * entry_px, info),
            status="open",
            opened_at=self.clock.now(),
            bars_open=0,
        )
        await self.repo.save_position(pos)
        if not await self.place_stop(pos, pos.amount, pos.stop_loss, 0):
            await self.emergency_close(pos, "stop_failed")
            return None
        return pos

    def _fee(self, notional: Decimal, info: MarketInfo | None) -> Decimal:
        return notional * (info.taker_fee if info else Decimal("0.001"))

    # ---------------------------------------------------------------- protection
    async def place_stop(self, pos: PositionRow, qty: Decimal, stop: Decimal, n: int) -> bool:
        long = pos.side == PositionSide.LONG.value
        info = self.market(pos.symbol)
        if info is not None:
            stop = round_price(stop, info.precision, Side.SELL if long else Side.BUY)
            qty = round_amount(qty, info.precision)
        if qty <= 0:
            return True
        req = OrderRequest(
            client_order_id=self._cid(pos.position_id, "stop", n),
            exchange=pos.exchange,
            symbol=pos.symbol,
            side=Side.SELL if long else Side.BUY,
            order_type=OrderType.STOP_MARKET,
            amount=qty,
            stop_price=stop,
            reduce_only=self.adapter.market_type != "spot",
            intent_id=pos.position_id,
        )
        try:
            order = await self.submit(req, "stop", pos.position_id, pos.strategy)
        except ExchangeAdapterError as exc:
            await self.alert(
                AlertLevel.CRITICAL,
                "stop_order_failed",
                f"{pos.symbol} stop emri konamadı: {exc}",
                position=pos.position_id,
            )
            return False
        if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            await self.alert(
                AlertLevel.CRITICAL,
                "stop_order_rejected",
                f"{pos.symbol} stop emri reddedildi ({order.status}).",
            )
            return False
        pos.stop_order_id = req.client_order_id
        pos.stop_loss = stop
        await self.repo.save_position(pos)
        if order.status is OrderStatus.FILLED:  # triggered immediately
            await self.finalize_stop(pos, order)
        return True

    async def cancel_stop(self, pos: PositionRow) -> Order | None:
        """Cancel the exchange stop; returns its final state (FILLED means it already hit)."""
        if not pos.stop_order_id:
            return None
        try:
            o = await self.adapter.fetch_order_by_client_id(pos.stop_order_id, pos.symbol)
        except OrderNotFoundError:
            return None
        if not o.status.is_terminal:
            try:
                await self.adapter.cancel_order(
                    o.exchange_order_id or o.client_order_id, pos.symbol
                )
            except ExchangeAdapterError as exc:
                _log.warning("stop_cancel_failed", error=str(exc))
            o = await self.refresh(o)
        await self.repo.save_order(o, "stop", pos.position_id)
        return o

    async def finalize_stop(self, pos: PositionRow, stop_order: Order) -> None:
        """The exchange stop filled: book the exit and close the position."""
        px = stop_order.average_price or stop_order.stop_price or pos.stop_loss
        await self._book_exit(
            pos, stop_order.filled or pos.amount, px, "trailing_stop" if pos.targets_hit else "stop"
        )

    async def _book_exit(self, pos: PositionRow, qty: Decimal, px: Decimal, reason: str) -> Decimal:
        long = pos.side == PositionSide.LONG.value
        qty = min(qty, pos.amount)
        gross = (px - pos.entry_price) * qty if long else (pos.entry_price - px) * qty
        fee = self._fee(px * qty, self.market(pos.symbol))
        pos.amount -= qty
        pos.realized_pnl += gross - fee
        pos.fees += fee
        if pos.amount <= 0:
            pos.status = "closed"
            pos.close_reason = reason
            pos.closed_at = self.clock.now()
            pos.stop_order_id = None
        await self.repo.save_position(pos)
        return gross - fee

    # ---------------------------------------------------------------- exits
    async def reduce(
        self,
        pos: PositionRow,
        approved: ApprovedIntent,
        reason: str,
        new_stop: Decimal | None = None,
    ) -> Decimal:
        """Market-reduce `approved.intent.amount`, then re-protect the remainder."""
        intent = approved.intent
        if not intent.reduce_only:
            raise ValueError("Çıkış emri reduce_only olmalı.")
        prior = await self.cancel_stop(pos)
        if prior is not None and prior.status is OrderStatus.FILLED:
            await self.finalize_stop(pos, prior)  # stop hit first: nothing left to reduce
            return pos.realized_pnl
        n = len(pos.take_profits) + pos.targets_hit + int(self.clock.now().timestamp())
        req = OrderRequest(
            client_order_id=self._cid(intent.intent_id, f"exit:{reason}", n),
            exchange=pos.exchange,
            symbol=pos.symbol,
            side=intent.side,
            order_type=OrderType.MARKET,
            amount=min(intent.amount, pos.amount),
            reduce_only=self.adapter.market_type != "spot",
            intent_id=intent.intent_id,
        )
        order = await self.submit(req, "exit", pos.position_id, pos.strategy)
        order = await self.await_fill(order, "exit", pos.position_id)
        px = order.average_price or ZERO
        pnl = await self._book_exit(pos, order.filled, px, reason) if order.filled > 0 else ZERO
        if pos.status == "open":
            stop = new_stop if new_stop is not None else pos.stop_loss
            if not await self.place_stop(pos, pos.amount, stop, n):
                await self.emergency_close(pos, "stop_failed")
        return pnl

    async def move_stop(self, pos: PositionRow, new_stop: Decimal) -> bool:
        prior = await self.cancel_stop(pos)
        if prior is not None and prior.status is OrderStatus.FILLED:
            await self.finalize_stop(pos, prior)
            return False
        n = int(self.clock.now().timestamp())
        if not await self.place_stop(pos, pos.amount, new_stop, n):
            await self.emergency_close(pos, "stop_failed")
            return False
        return True

    async def emergency_close(self, pos: PositionRow, reason: str) -> None:
        """Close at market immediately (rule 9: no position without an exchange stop)."""
        long = pos.side == PositionSide.LONG.value
        req = OrderRequest(
            client_order_id=self._cid(
                pos.position_id, f"emergency:{reason}", int(self.clock.now().timestamp())
            ),
            exchange=pos.exchange,
            symbol=pos.symbol,
            side=Side.SELL if long else Side.BUY,
            order_type=OrderType.MARKET,
            amount=pos.amount,
            reduce_only=self.adapter.market_type != "spot",
            intent_id=pos.position_id,
        )
        try:
            order = await self.submit(req, "exit", pos.position_id, pos.strategy)
            order = await self.await_fill(order, "exit", pos.position_id)
            if order.filled > 0:
                await self._book_exit(pos, order.filled, order.average_price or ZERO, reason)
        finally:
            await self.alert(
                AlertLevel.CRITICAL,
                f"emergency_close_{reason}",
                f"{pos.symbol} pozisyonu acil kapatıldı ({reason}); kalan: {pos.amount}.",
                position=pos.position_id,
            )

    async def sync_stop(self, pos: PositionRow) -> bool:
        """Poll the exchange stop; True if the position was closed by it."""
        if not pos.stop_order_id:
            return False
        try:
            o = await self.adapter.fetch_order_by_client_id(pos.stop_order_id, pos.symbol)
        except OrderNotFoundError:
            return False
        if o.status is OrderStatus.FILLED:
            await self.repo.save_order(o, "stop", pos.position_id)
            await self.finalize_stop(pos, o)
            return True
        if o.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
            await self.alert(
                AlertLevel.WARNING,
                "stop_missing",
                f"{pos.symbol} borsa stop emri yok ({o.status}); yeniden konuyor.",
            )
            if not await self.place_stop(
                pos, pos.amount, pos.stop_loss, int(self.clock.now().timestamp())
            ):
                await self.emergency_close(pos, "stop_failed")
        return False
