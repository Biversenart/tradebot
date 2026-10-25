"""ORM tables (spec §5.8)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bot.storage.types import DecimalString, UtcDateTime


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))  # entry | stop | exit
    amount: Mapped[Decimal] = mapped_column(DecimalString)
    price: Mapped[Decimal | None] = mapped_column(DecimalString)
    stop_price: Mapped[Decimal | None] = mapped_column(DecimalString)
    status: Mapped[str] = mapped_column(String(20), index=True)
    filled: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    average_price: Mapped[Decimal | None] = mapped_column(DecimalString)
    intent_id: Mapped[str | None] = mapped_column(String(64))
    position_id: Mapped[str | None] = mapped_column(String(64), index=True)
    strategy: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class TradeRow(Base):
    """Executions (fills)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64))
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(DecimalString)
    amount: Mapped[Decimal] = mapped_column(DecimalString)
    fee: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    fee_currency: Mapped[str | None] = mapped_column(String(16))
    is_maker: Mapped[bool] = mapped_column(Boolean, default=False)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime)

    __table_args__ = (Index("ix_trades_exchange_trade", "exchange", "trade_id", unique=True),)


class PositionRow(Base):
    __tablename__ = "positions"

    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8), default="1h")
    amount: Mapped[Decimal] = mapped_column(DecimalString)  # remaining
    initial_amount: Mapped[Decimal] = mapped_column(DecimalString)
    entry_price: Mapped[Decimal] = mapped_column(DecimalString)
    initial_stop: Mapped[Decimal] = mapped_column(DecimalString)
    stop_loss: Mapped[Decimal] = mapped_column(DecimalString)
    stop_order_id: Mapped[str | None] = mapped_column(String(64))  # client_order_id of the stop
    take_profits: Mapped[list[str]] = mapped_column(JSON, default=list)
    targets_hit: Mapped[int] = mapped_column(Integer, default=0)
    best_price: Mapped[Decimal | None] = mapped_column(DecimalString)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(16), index=True, default="open")  # open|closed
    close_reason: Mapped[str | None] = mapped_column(String(32))
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    bars_open: Mapped[int] = mapped_column(Integer, default=0)


class SignalRow(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    score: Mapped[Decimal] = mapped_column(DecimalString)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime)


class RiskReportRow(Base):
    __tablename__ = "risk_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("signals.signal_id"))
    symbol: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(16))
    risk_score: Mapped[str] = mapped_column(String(8))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class BalanceSnapshotRow(Base):
    __tablename__ = "balances_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32))
    asset: Mapped[str] = mapped_column(String(16))
    free: Mapped[Decimal] = mapped_column(DecimalString)
    used: Mapped[Decimal] = mapped_column(DecimalString)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class ArbitrageOpportunityRow(Base):
    __tablename__ = "arbitrage_opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # cross | triangular
    route: Mapped[str] = mapped_column(String(128))
    net_pct: Mapped[Decimal] = mapped_column(DecimalString)
    size_quote: Mapped[Decimal] = mapped_column(DecimalString)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class RiskEventRow(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class EquityPointRow(Base):
    __tablename__ = "equity_curve"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equity: Mapped[Decimal] = mapped_column(DecimalString)
    reserve: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal(0))
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)


class StateRow(Base):
    """Small key/value store (kill switch state, last reconciliation, ...)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
