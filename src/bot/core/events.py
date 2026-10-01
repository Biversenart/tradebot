"""Events carried on the internal EventBus (spec §3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from bot.core.models import Candle, Fill, Order, OrderBook, OrderIntent, Signal, Ticker


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """Base class of all bus events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_at: AwareDatetime = Field(default_factory=_utcnow)


class TickerEvent(Event):
    ticker: Ticker


class CandleEvent(Event):
    candle: Candle


class OrderBookEvent(Event):
    order_book: OrderBook


class SignalEvent(Event):
    signal: Signal


class OrderIntentEvent(Event):
    """A strategy's intent. Only RiskManager may turn it into an order."""

    intent: OrderIntent


class OrderUpdate(Event):
    order: Order


class FillEvent(Event):
    fill: Fill


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RiskAlert(Event):
    level: AlertLevel
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class PositionEvent(Event):
    """Position lifecycle notification (Telegram / panel)."""

    kind: str  # opened | reduced | closed
    position_id: str
    exchange: str
    symbol: str
    side: str
    amount: Decimal
    price: Decimal | None = None
    realized_pnl: Decimal = Decimal(0)
    reason: str = ""
