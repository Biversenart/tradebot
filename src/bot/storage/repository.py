"""Repository: the only place that talks SQL. Domain objects in, domain objects out."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.core.events import RiskAlert
from bot.core.models import Balance, Fill, Order, OrderStatus, Signal
from bot.storage.db import create_all, make_engine, session_factory
from bot.storage.models import (
    ArbitrageOpportunityRow,
    BalanceSnapshotRow,
    EquityPointRow,
    OrderRow,
    PositionRow,
    RiskEventRow,
    RiskReportRow,
    SignalRow,
    StateRow,
    TradeRow,
)

OPEN_STATUSES = (OrderStatus.NEW.value, OrderStatus.OPEN.value, OrderStatus.PARTIALLY_FILLED.value)


def _now() -> datetime:
    return datetime.now(UTC)


class Repository:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = session_factory(engine)

    @classmethod
    async def connect(cls, url: str, create: bool = True) -> Repository:
        engine = make_engine(url)
        if create:
            await create_all(engine)
        return cls(engine)

    async def close(self) -> None:
        await self.engine.dispose()

    # ---------------------------------------------------------------- orders
    async def save_order(
        self, order: Order, role: str, position_id: str | None = None, strategy: str = ""
    ) -> None:
        async with self._sessions.begin() as s:
            row = (
                await s.execute(
                    select(OrderRow).where(OrderRow.client_order_id == order.client_order_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = OrderRow(
                    client_order_id=order.client_order_id,
                    role=role,
                    position_id=position_id,
                    strategy=strategy,
                    created_at=order.created_at,
                )
                s.add(row)
            row.exchange_order_id = order.exchange_order_id
            row.exchange = order.exchange
            row.symbol = order.symbol
            row.side = order.side.value
            row.order_type = order.order_type.value
            row.amount = order.amount
            row.price = order.price
            row.stop_price = order.stop_price
            row.status = order.status.value
            row.filled = order.filled
            row.average_price = order.average_price
            row.intent_id = order.intent_id
            if position_id is not None:
                row.position_id = position_id
            row.updated_at = order.updated_at or _now()

    async def get_order(self, client_order_id: str) -> OrderRow | None:
        async with self._sessions() as s:
            return (
                await s.execute(select(OrderRow).where(OrderRow.client_order_id == client_order_id))
            ).scalar_one_or_none()

    async def open_orders(self, exchange: str | None = None) -> list[OrderRow]:
        async with self._sessions() as s:
            q = select(OrderRow).where(OrderRow.status.in_(OPEN_STATUSES))
            if exchange:
                q = q.where(OrderRow.exchange == exchange)
            return list((await s.execute(q)).scalars())

    # ---------------------------------------------------------------- fills
    async def save_fill(self, fill: Fill) -> bool:
        async with self._sessions.begin() as s:
            exists = (
                await s.execute(
                    select(TradeRow.id).where(
                        TradeRow.exchange == fill.exchange, TradeRow.trade_id == fill.trade_id
                    )
                )
            ).first()
            if exists:
                return False
            s.add(
                TradeRow(
                    trade_id=fill.trade_id,
                    client_order_id=fill.client_order_id,
                    exchange=fill.exchange,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    price=fill.price,
                    amount=fill.amount,
                    fee=fill.fee,
                    fee_currency=fill.fee_currency,
                    is_maker=fill.is_maker,
                    timestamp=fill.timestamp,
                )
            )
            return True

    # ---------------------------------------------------------------- positions
    async def save_position(self, row: PositionRow) -> None:
        async with self._sessions.begin() as s:
            await s.merge(row)

    async def get_position(self, position_id: str) -> PositionRow | None:
        async with self._sessions() as s:
            return await s.get(PositionRow, position_id)

    async def open_positions(self, exchange: str | None = None) -> list[PositionRow]:
        async with self._sessions() as s:
            q = select(PositionRow).where(PositionRow.status == "open")
            if exchange:
                q = q.where(PositionRow.exchange == exchange)
            return list((await s.execute(q)).scalars())

    async def closed_positions(self, limit: int = 100) -> list[PositionRow]:
        async with self._sessions() as s:
            q = (
                select(PositionRow)
                .where(PositionRow.status == "closed")
                .order_by(PositionRow.closed_at.desc())
                .limit(limit)
            )
            return list((await s.execute(q)).scalars())

    # ---------------------------------------------------------------- analytics
    async def save_signal(self, signal: Signal) -> None:
        plan = json.loads(signal.plan.model_dump_json()) if signal.plan else None
        async with self._sessions.begin() as s:
            await s.merge(
                SignalRow(
                    signal_id=signal.signal_id,
                    strategy=signal.strategy,
                    exchange=signal.exchange,
                    symbol=signal.symbol,
                    timeframe=signal.timeframe,
                    side=signal.side.value,
                    score=signal.score,
                    plan=plan,
                    timestamp=signal.timestamp,
                )
            )

    async def save_risk_report(self, report: dict[str, Any], signal_id: str | None) -> None:
        async with self._sessions.begin() as s:
            s.add(
                RiskReportRow(
                    signal_id=signal_id,
                    symbol=str(report.get("symbol")),
                    decision=str(report.get("decision")),
                    risk_score=str(report.get("score")),
                    data=report,
                    created_at=_now(),
                )
            )

    async def save_risk_event(self, alert: RiskAlert) -> None:
        details = json.loads(json.dumps(alert.details, default=str))
        async with self._sessions.begin() as s:
            s.add(
                RiskEventRow(
                    level=alert.level.value,
                    code=alert.code,
                    message=alert.message,
                    details=details,
                    timestamp=alert.created_at,
                )
            )

    async def recent_risk_events(self, limit: int = 50) -> list[RiskEventRow]:
        async with self._sessions() as s:
            q = select(RiskEventRow).order_by(RiskEventRow.timestamp.desc()).limit(limit)
            return list((await s.execute(q)).scalars())

    async def save_equity(
        self, equity: Decimal, reserve: Decimal = Decimal(0), when: datetime | None = None
    ) -> None:
        async with self._sessions.begin() as s:
            s.add(EquityPointRow(equity=equity, reserve=reserve, timestamp=when or _now()))

    async def equity_curve(self, limit: int = 5000) -> list[EquityPointRow]:
        async with self._sessions() as s:
            q = select(EquityPointRow).order_by(EquityPointRow.timestamp.desc()).limit(limit)
            return list(reversed(list((await s.execute(q)).scalars())))

    async def save_balances(self, exchange: str, balances: dict[str, Balance]) -> None:
        now = _now()
        async with self._sessions.begin() as s:
            for b in balances.values():
                s.add(
                    BalanceSnapshotRow(
                        exchange=exchange, asset=b.asset, free=b.free, used=b.used, timestamp=now
                    )
                )

    async def save_arbitrage(
        self,
        kind: str,
        route: str,
        net_pct: Decimal,
        size_quote: Decimal,
        executed: bool,
        data: dict[str, Any],
    ) -> None:
        async with self._sessions.begin() as s:
            s.add(
                ArbitrageOpportunityRow(
                    kind=kind,
                    route=route,
                    net_pct=net_pct,
                    size_quote=size_quote,
                    executed=executed,
                    data=json.loads(json.dumps(data, default=str)),
                    timestamp=_now(),
                )
            )

    # ---------------------------------------------------------------- key/value state
    async def get_state(self, key: str) -> dict[str, Any] | None:
        async with self._sessions() as s:
            row = await s.get(StateRow, key)
            return dict(row.value) if row else None

    async def set_state(self, key: str, value: dict[str, Any]) -> None:
        async with self._sessions.begin() as s:
            await s.merge(StateRow(key=key, value=value, updated_at=_now()))
