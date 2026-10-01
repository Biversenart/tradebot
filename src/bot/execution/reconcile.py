"""Reconciliation on (re)start (spec §5.7): make internal state match the exchange.

1. Orders the DB thinks are open: refresh from the exchange (filled / cancelled while down).
   An entry that filled while the bot was down becomes a position and gets a stop.
2. Exchange open orders carrying our client-id prefix but unknown to the DB are adopted;
   foreign orders are only reported (never touched).
3. Every open position must have a live exchange stop: a filled stop closes the position,
   a missing/cancelled stop is re-placed, and if that fails the position is closed (rule 9).
4. Spot: if the free+used base balance is below the position size, the position is reduced to
   what is actually held (manual intervention suspected) and an alert is raised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bot.core.events import AlertLevel
from bot.core.models import Order, OrderStatus, OrderType, PositionSide, Side
from bot.exchanges.errors import ExchangeAdapterError, OrderNotFoundError
from bot.execution.engine import ExecutionEngine
from bot.storage.models import OrderRow, PositionRow

ZERO = Decimal(0)


@dataclass
class ReconcileReport:
    refreshed_orders: list[str] = field(default_factory=list)
    adopted_orders: list[str] = field(default_factory=list)
    foreign_orders: list[str] = field(default_factory=list)
    positions_created: list[str] = field(default_factory=list)
    positions_closed: list[str] = field(default_factory=list)
    stops_replaced: list[str] = field(default_factory=list)
    positions_adjusted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(
            (
                self.adopted_orders,
                self.positions_created,
                self.positions_closed,
                self.stops_replaced,
                self.positions_adjusted,
            )
        )

    def summary_tr(self) -> str:
        return (
            f"güncellenen emir {len(self.refreshed_orders)}, "
            f"sahiplenilen {len(self.adopted_orders)}, "
            f"yabancı {len(self.foreign_orders)}, yeni pozisyon {len(self.positions_created)}, "
            f"kapanan {len(self.positions_closed)}, yeniden konan stop {len(self.stops_replaced)}, "
            f"düzeltilen {len(self.positions_adjusted)}, hata {len(self.errors)}"
        )


def _row_to_order(row: OrderRow) -> Order:
    return Order(
        client_order_id=row.client_order_id,
        exchange_order_id=row.exchange_order_id,
        exchange=row.exchange,
        symbol=row.symbol,
        side=Side(row.side),
        order_type=OrderType(row.order_type),
        amount=row.amount,
        price=row.price,
        stop_price=row.stop_price,
        status=OrderStatus(row.status),
        filled=row.filled,
        average_price=row.average_price,
        intent_id=row.intent_id,
        created_at=row.created_at,
    )


async def reconcile(engine: ExecutionEngine) -> ReconcileReport:
    rep = ReconcileReport()
    ad, repo = engine.adapter, engine.repo
    prefix = engine.cfg.client_prefix

    # 1) refresh orders the DB believes are open
    for row in await repo.open_orders(ad.name):
        try:
            fresh = await engine.refresh(_row_to_order(row))
        except OrderNotFoundError:
            rep.errors.append(f"emir bulunamadı: {row.client_order_id}")
            continue
        except ExchangeAdapterError as exc:
            rep.errors.append(f"{row.client_order_id}: {exc}")
            continue
        await repo.save_order(fresh, row.role, row.position_id, row.strategy)
        rep.refreshed_orders.append(row.client_order_id)
        if (
            row.role == "entry"
            and fresh.filled > 0
            and row.position_id
            and await repo.get_position(row.position_id) is None
        ):
            rep.errors.append(
                f"{row.symbol}: giriş kapalıyken doldu ama plan verisi yok "
                f"({row.client_order_id}); pozisyon acil kapatılıyor"
            )
            pos = PositionRow(
                position_id=row.position_id,
                exchange=row.exchange,
                symbol=row.symbol,
                side=(PositionSide.LONG if row.side == "buy" else PositionSide.SHORT).value,
                strategy=row.strategy,
                amount=fresh.filled,
                initial_amount=fresh.filled,
                entry_price=fresh.average_price or row.price or ZERO,
                initial_stop=ZERO,
                stop_loss=ZERO,
                take_profits=[],
                status="open",
                opened_at=row.created_at,
            )
            await repo.save_position(pos)
            rep.positions_created.append(pos.position_id)
            await engine.emergency_close(pos, "reconcile_no_stop")

    # 2) adopt our orphan orders, report foreign ones
    try:
        exchange_open = await ad.fetch_open_orders()
    except ExchangeAdapterError as exc:
        rep.errors.append(f"açık emirler alınamadı: {exc}")
        exchange_open = []
    for o in exchange_open:
        if await repo.get_order(o.client_order_id) is not None:
            continue
        if o.client_order_id.startswith(prefix):
            await repo.save_order(o, "unknown")
            rep.adopted_orders.append(o.client_order_id)
        else:
            rep.foreign_orders.append(o.client_order_id)

    # 3-4) every open position: real stop + real balance
    balances: dict[str, Decimal] = {}
    if ad.market_type == "spot":
        try:
            balances = {a: b.total for a, b in (await ad.fetch_balance()).items()}
        except ExchangeAdapterError as exc:
            rep.errors.append(f"bakiye alınamadı: {exc}")
    for pos in await repo.open_positions(ad.name):
        if pos.side == PositionSide.LONG.value and balances:
            info = engine.market(pos.symbol)
            if info is not None:
                held = balances.get(info.base, ZERO)
                if held < pos.amount:
                    rep.positions_adjusted.append(pos.position_id)
                    await engine.alert(
                        AlertLevel.WARNING,
                        "position_balance_mismatch",
                        f"{pos.symbol}: bakiye {held} < pozisyon {pos.amount}; düzeltildi.",
                    )
                    pos.amount = held
                    if held <= 0:
                        pos.status, pos.close_reason = "closed", "reconcile_missing_balance"
                        pos.closed_at = engine.clock.now()
                        await repo.save_position(pos)
                        rep.positions_closed.append(pos.position_id)
                        continue
                    await repo.save_position(pos)
        if not pos.stop_order_id:
            if pos.stop_loss > 0 and await engine.place_stop(
                pos, pos.amount, pos.stop_loss, int(engine.clock.now().timestamp())
            ):
                rep.stops_replaced.append(pos.position_id)
            else:
                await engine.emergency_close(pos, "reconcile_no_stop")
                rep.positions_closed.append(pos.position_id)
            continue
        old_stop_id = pos.stop_order_id
        try:
            closed = await engine.sync_stop(pos)
        except ExchangeAdapterError as exc:
            rep.errors.append(f"{pos.symbol} stop kontrolü: {exc}")
            continue
        if closed:
            rep.positions_closed.append(pos.position_id)
        else:
            fresh_pos = await repo.get_position(pos.position_id)
            if fresh_pos is not None and fresh_pos.stop_order_id != old_stop_id:
                rep.stops_replaced.append(pos.position_id)
    level = AlertLevel.WARNING if rep.changed or rep.errors else AlertLevel.INFO
    await engine.alert(level, "reconcile_done", "Reconciliation: " + rep.summary_tr())
    return rep
