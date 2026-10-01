"""ArbitrageService: order books -> scanners -> log/DB (+ paper execution) (spec §5.5)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal

from bot.arbitrage.cross import Venue, fx_mid, scan_cross
from bot.arbitrage.executor import ArbitrageExecutor
from bot.arbitrage.models import Opportunity
from bot.arbitrage.rebalance import rebalance_advice
from bot.arbitrage.triangular import Market, scan_triangular
from bot.config.schema import ArbitrageConfig
from bot.core.aio import wait_or_stop
from bot.core.clock import Clock, SystemClock
from bot.core.event_bus import EventBus
from bot.core.events import AlertLevel, OrderBookEvent, RiskAlert
from bot.core.models import OrderBook
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import ExchangeAdapterError
from bot.log import get_logger
from bot.storage.repository import Repository

ZERO = Decimal(0)
_log = get_logger(__name__)


def venue_symbol(cfg: ArbitrageConfig, exchange: str, pair: str) -> str:
    fx = cfg.cross_exchange.fx_symbols.get(exchange)
    if fx is None:
        return pair
    base = pair.split("/")[0]
    return f"{base}/{fx.split('/')[1]}"  # e.g. BTC/USDT -> BTC/TRY via USDT/TRY


def arbitrage_symbols(cfg: ArbitrageConfig, exchange: str) -> set[str]:
    """Extra market-data symbols the arbitrage scanners need on `exchange`."""
    out: set[str] = set()
    cx = cfg.cross_exchange
    if cx.enabled:
        out |= {venue_symbol(cfg, exchange, p) for p in cx.pairs}
        if exchange in cx.fx_symbols:
            out.add(cx.fx_symbols[exchange])
    tri = cfg.triangular
    if tri.enabled and tri.exchange == exchange:
        out |= set(tri.symbols)
    return out


class ArbitrageService:
    def __init__(
        self,
        cfg: ArbitrageConfig,
        bus: EventBus,
        adapters: dict[str, ExchangeAdapter],
        repo: Repository | None = None,
        executor: ArbitrageExecutor | None = None,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.adapters = adapters
        self.repo = repo
        self.executor = executor
        self.clock = clock or SystemClock()
        self._mono = monotonic
        self.books: dict[tuple[str, str], OrderBook] = {}
        self.balances: dict[str, dict[str, Decimal]] = {}
        self._last_logged: dict[str, float] = {}
        self.found: list[Opportunity] = []
        self._busy = False
        bus.subscribe(OrderBookEvent, self.on_book)

    # ---------------------------------------------------------------- symbols to watch
    def watched_symbols(self, exchange: str) -> set[str]:
        return arbitrage_symbols(self.cfg, exchange)

    def _venue_symbol(self, exchange: str, pair: str) -> str:
        return venue_symbol(self.cfg, exchange, pair)

    # ---------------------------------------------------------------- inventory
    async def refresh_balances(self) -> None:
        for name, ad in self.adapters.items():
            try:
                bal = await ad.fetch_balance()
            except ExchangeAdapterError as exc:
                _log.warning("arb_balance_failed", exchange=name, error=str(exc))
                continue
            self.balances[name] = {a: b.free for a, b in bal.items()}

    # ---------------------------------------------------------------- events
    async def on_book(self, event: OrderBookEvent) -> None:
        ob = event.order_book
        self.books[(ob.exchange, ob.symbol)] = ob
        now = self.clock.now()
        opps: list[Opportunity] = []
        cx = self.cfg.cross_exchange
        if cx.enabled:
            for pair in cx.pairs:
                venues = self._venues(pair)
                if len(venues) >= 2:
                    opps += scan_cross(venues, cx, now)
        tri = self.cfg.triangular
        if tri.enabled and ob.exchange == tri.exchange:
            markets = self._markets(tri.exchange)
            if len(markets) >= 3:
                opps += scan_triangular(markets, tri, now, tri.exchange)
        for opp in opps:
            await self._handle(opp)

    def _fee(self, exchange: str, symbol: str) -> Decimal:
        try:
            return self.adapters[exchange].market_info(symbol).taker_fee
        except (ExchangeAdapterError, KeyError):
            return Decimal("0.001")

    def _venues(self, pair: str) -> dict[str, Venue]:
        cx = self.cfg.cross_exchange
        out: dict[str, Venue] = {}
        for name in self.adapters:
            sym = self._venue_symbol(name, pair)
            book = self.books.get((name, sym))
            if book is None:
                continue
            fx = Decimal(1)
            if name in cx.fx_symbols:
                fx_book = self.books.get((name, cx.fx_symbols[name]))
                mid = fx_mid(fx_book) if fx_book else None
                if not mid:
                    continue
                fx = 1 / mid  # venue quote (TRY) -> common quote (USDT)
            base, quote = sym.split("/")
            bal = self.balances.get(name, {})
            out[name] = Venue(
                name,
                sym,
                book,
                self._fee(name, sym),
                fx,
                bal.get(quote) if bal else None,
                bal.get(base) if bal else None,
            )
        return out

    def _markets(self, exchange: str) -> dict[str, Market]:
        out = {}
        for (ex, sym), book in self.books.items():
            if ex != exchange or sym not in self.cfg.triangular.symbols:
                continue
            base, quote = sym.split("/")
            out[sym] = Market(sym, base, quote, book, self._fee(ex, sym))
        return out

    async def _handle(self, opp: Opportunity) -> None:
        now = self._mono()
        if now - self._last_logged.get(opp.route, -1e9) < self.cfg.min_interval_seconds:
            return
        self._last_logged[opp.route] = now
        self.found.append(opp)
        self.found = self.found[-500:]
        _log.info(
            "arbitrage_opportunity",
            summary=opp.summary_tr,
            latency_ms=round(opp.latency_ms, 1),
            depth_limited=opp.depth_limited,
        )
        executed = False
        if self.executor is not None and self.cfg.execute_in_paper and not self._busy:
            self._busy = True
            try:
                result = await self.executor.execute(opp)
                executed = result.status in ("completed", "hedged")
                if result.status == "hedged":
                    await self.bus.publish(
                        RiskAlert(
                            level=AlertLevel.WARNING,
                            code="arbitrage_leg_hedged",
                            message=f"Arbitraj bacağı hedge edildi: {opp.route}",
                            details={"reasons": result.reasons},
                        )
                    )
                await self.refresh_balances()
                await self._rebalance_check()
            finally:
                self._busy = False
        if self.repo is not None:
            await self.repo.save_arbitrage(
                opp.kind.value,
                opp.route,
                opp.net_pct,
                opp.notional_quote,
                executed,
                {
                    "gross_pct": str(opp.gross_pct),
                    "latency_ms": opp.latency_ms,
                    "depth_limited": opp.depth_limited,
                    "legs": [
                        {
                            "exchange": lg.exchange,
                            "symbol": lg.symbol,
                            "side": lg.side.value,
                            "amount": str(lg.amount),
                            "avg": str(lg.avg_price),
                        }
                        for lg in opp.legs
                    ],
                },
            )

    async def _rebalance_check(self) -> None:
        cx = self.cfg.cross_exchange
        if not cx.enabled:
            return
        for pair in cx.pairs:
            base, quote = pair.split("/")
            inv: dict[str, dict[str, Decimal]] = {}
            for name, bal in self.balances.items():
                sym = self._venue_symbol(name, pair)
                book = self.books.get((name, sym))
                mid = fx_mid(book) if book else None
                if mid is None:
                    continue
                vquote = sym.split("/")[1]
                inv[name] = {base: bal.get(base, ZERO) * mid, quote: bal.get(vquote, ZERO)}
            for adv in rebalance_advice(inv, base, quote, self.cfg.rebalance.min_share_pct):
                await self.bus.publish(
                    RiskAlert(
                        level=AlertLevel.WARNING, code="arbitrage_rebalance", message=adv.message
                    )
                )

    async def run(self, stop: asyncio.Event, balance_interval: float = 60.0) -> None:
        while not stop.is_set():
            await self.refresh_balances()
            await self._rebalance_check()
            await wait_or_stop(stop, balance_interval)
