"""Domain models. All monetary values are `Decimal`; all timestamps are timezone-aware."""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is PositionSide.LONG else Side.SELL

    @property
    def exit_side(self) -> Side:
        return Side.SELL if self is PositionSide.LONG else Side.BUY


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    NEW = "new"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


class Horizon(StrEnum):
    SCALP = "scalp"
    SWING = "swing"
    POSITION = "position"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _new_id() -> str:
    return uuid.uuid4().hex


class MarketPrecision(_Frozen):
    """Exchange trading rules for a symbol."""

    tick_size: Decimal = Field(gt=0)
    lot_size: Decimal = Field(gt=0)
    min_notional: Decimal = Field(default=Decimal(0), ge=0)


# --------------------------------------------------------------------------- market data


class Candle(_Frozen):
    exchange: str
    symbol: str
    timeframe: str
    open_time: AwareDatetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    closed: bool = True

    @model_validator(mode="after")
    def _check_ohlc(self) -> Candle:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC tutarsız: low <= open,close <= high olmalı.")
        return self


class Ticker(_Frozen):
    exchange: str
    symbol: str
    timestamp: AwareDatetime
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    last: Decimal = Field(gt=0)
    bid_volume: Decimal | None = Field(default=None, ge=0)
    ask_volume: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_not_crossed(self) -> Ticker:
        if self.bid > self.ask:
            raise ValueError("bid, ask'ten büyük olamaz.")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        """Spread as a percentage of mid price."""
        return self.spread / self.mid * 100


class OrderBookLevel(_Frozen):
    price: Decimal = Field(gt=0)
    amount: Decimal = Field(gt=0)


class OrderBook(_Frozen):
    """L2 order book. Bids sorted descending, asks ascending."""

    exchange: str
    symbol: str
    timestamp: AwareDatetime
    bids: tuple[OrderBookLevel, ...] = ()
    asks: tuple[OrderBookLevel, ...] = ()

    @model_validator(mode="after")
    def _check_sorted(self) -> OrderBook:
        bid_prices = [lvl.price for lvl in self.bids]
        ask_prices = [lvl.price for lvl in self.asks]
        if bid_prices != sorted(bid_prices, reverse=True):
            raise ValueError("bids azalan fiyat sırasında olmalı.")
        if ask_prices != sorted(ask_prices):
            raise ValueError("asks artan fiyat sırasında olmalı.")
        if bid_prices and ask_prices and bid_prices[0] > ask_prices[0]:
            raise ValueError("Orderbook kesişik: en iyi bid en iyi ask'ten büyük.")
        return self

    @property
    def best_bid(self) -> OrderBookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderBookLevel | None:
        return self.asks[0] if self.asks else None


# --------------------------------------------------------------------------- orders


class OrderIntent(_Frozen):
    """What a strategy wants to do. Must pass through RiskManager before reaching an exchange.

    Strategies only produce OrderIntents; they never send orders directly.
    """

    intent_id: str = Field(default_factory=_new_id)
    exchange: str
    symbol: str
    side: Side
    order_type: OrderType
    amount: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    reduce_only: bool = False
    strategy: str = "unknown"
    reason: str = ""

    @model_validator(mode="after")
    def _check_prices(self) -> OrderIntent:
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.price is None:
            raise ValueError("Limit emirlerinde price zorunludur.")
        if (
            self.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
            and self.stop_price is None
        ):
            raise ValueError("Stop emirlerinde stop_price zorunludur.")
        ref = self.price
        if ref is not None:
            below, above = (
                (self.stop_loss, self.take_profit)
                if self.side is Side.BUY
                else (self.take_profit, self.stop_loss)
            )
            if below is not None and below >= ref:
                raise ValueError("Stop-loss/take-profit giriş fiyatının yanlış tarafında.")
            if above is not None and above <= ref:
                raise ValueError("Stop-loss/take-profit giriş fiyatının yanlış tarafında.")
        return self


class Order(_Frozen):
    """An order as known by the exchange (or the paper exchange)."""

    client_order_id: str
    exchange_order_id: str | None = None
    exchange: str
    symbol: str
    side: Side
    order_type: OrderType
    amount: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    status: OrderStatus = OrderStatus.NEW
    filled: Decimal = Field(default=Decimal(0), ge=0)
    average_price: Decimal | None = Field(default=None, gt=0)
    intent_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_filled(self) -> Order:
        if self.filled > self.amount:
            raise ValueError("Dolum miktarı emir miktarını aşamaz.")
        return self

    @property
    def remaining(self) -> Decimal:
        return self.amount - self.filled


class Fill(_Frozen):
    trade_id: str
    client_order_id: str
    exchange: str
    symbol: str
    side: Side
    price: Decimal = Field(gt=0)
    amount: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal(0), ge=0)
    fee_currency: str | None = None
    is_maker: bool = False
    timestamp: AwareDatetime

    @property
    def notional(self) -> Decimal:
        return self.price * self.amount


class Position(_Frozen):
    """An open position. Every open position must have an exchange-side stop order."""

    position_id: str = Field(default_factory=_new_id)
    exchange: str
    symbol: str
    side: PositionSide
    amount: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    stop_order_id: str | None = None
    take_profits: tuple[Decimal, ...] = ()
    realized_pnl: Decimal = Decimal(0)
    opened_at: AwareDatetime
    strategy: str = "unknown"

    @model_validator(mode="after")
    def _check_stop_side(self) -> Position:
        if self.side is PositionSide.LONG and self.stop_loss >= self.entry_price:
            raise ValueError("Long pozisyonda stop-loss giriş fiyatının altında olmalı.")
        if self.side is PositionSide.SHORT and self.stop_loss <= self.entry_price:
            raise ValueError("Short pozisyonda stop-loss giriş fiyatının üstünde olmalı.")
        return self

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        diff = mark_price - self.entry_price
        if self.side is PositionSide.SHORT:
            diff = -diff
        return diff * self.amount

    @property
    def risk_at_stop(self) -> Decimal:
        """Loss (positive number) if the stop is hit, excluding fees and slippage."""
        return abs(self.entry_price - self.stop_loss) * self.amount


# --------------------------------------------------------------------------- analysis output


class TradePlan(_Frozen):
    """Full trade plan produced by the analysis engine (spec §5.3.i)."""

    plan_id: str = Field(default_factory=_new_id)
    symbol: str
    exchange: str
    side: PositionSide
    horizon: Horizon
    timeframe: str
    entry_low: Decimal = Field(gt=0)
    entry_high: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    take_profits: tuple[Decimal, ...] = Field(min_length=1, max_length=3)
    confluence_score: Decimal = Field(ge=0, le=100)
    reasons: tuple[str, ...] = ()
    invalidation: str
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _check_levels(self) -> TradePlan:
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low, entry_high'tan büyük olamaz.")
        tps = list(self.take_profits)
        if self.side is PositionSide.LONG:
            if self.stop_loss >= self.entry_low:
                raise ValueError("Long planda stop giriş bölgesinin altında olmalı.")
            if tps != sorted(tps) or tps[0] <= self.entry_high:
                raise ValueError("Long planda TP'ler girişin üstünde ve artan sırada olmalı.")
        else:
            if self.stop_loss <= self.entry_high:
                raise ValueError("Short planda stop giriş bölgesinin üstünde olmalı.")
            if tps != sorted(tps, reverse=True) or tps[0] >= self.entry_low:
                raise ValueError("Short planda TP'ler girişin altında ve azalan sırada olmalı.")
        return self

    @property
    def entry_mid(self) -> Decimal:
        return (self.entry_low + self.entry_high) / 2

    @property
    def risk_per_unit(self) -> Decimal:
        return abs(self.entry_mid - self.stop_loss)

    @property
    def reward_risk_ratios(self) -> tuple[Decimal, ...]:
        """R multiple of each TP measured from the middle of the entry zone."""
        risk = self.risk_per_unit
        return tuple(abs(tp - self.entry_mid) / risk for tp in self.take_profits)

    @property
    def risk_reward(self) -> Decimal:
        """R:R to the final target (used by the `min_risk_reward` filter)."""
        return self.reward_risk_ratios[-1]


class Signal(_Frozen):
    signal_id: str = Field(default_factory=_new_id)
    strategy: str
    exchange: str
    symbol: str
    timeframe: str
    side: PositionSide
    score: Decimal = Field(ge=0, le=100)
    timestamp: AwareDatetime
    plan: TradePlan | None = None
    reasons: tuple[str, ...] = ()
