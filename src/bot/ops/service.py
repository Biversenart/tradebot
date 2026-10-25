"""OpsService: builds the §5.13 safety nets and runs their periodic checks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bot.config.schema import AppConfig, Mode
from bot.core.clock import Clock, SystemClock
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import ExchangeAdapterError
from bot.log import get_logger
from bot.ops.alerts import AlertSink
from bot.ops.announcements import (
    AnnouncementSource,
    AnnouncementWatcher,
    BinanceAnnouncementSource,
    FileAnnouncementSource,
    HttpGet,
)
from bot.ops.balance import ExchangeBalanceCap
from bot.ops.capital import CapitalCap
from bot.ops.clock_skew import ClockSkewMonitor
from bot.ops.guard import OpsGuard
from bot.ops.liquidity import LowLiquidityMode
from bot.ops.stablecoin import DepegMonitor

_log = get_logger(__name__)


class OpsService:
    def __init__(
        self,
        cfg: AppConfig,
        mode: Mode,
        adapters: Mapping[str, ExchangeAdapter],
        alert_sink: AlertSink | None = None,
        http_get: HttpGet | None = None,
        clock: Clock | None = None,
        sources: list[AnnouncementSource] | None = None,
    ) -> None:
        ops = cfg.operations
        self.cfg = cfg
        self.adapters = adapters
        self.alert_sink = alert_sink
        self.clock = clock or SystemClock()
        self.capital = CapitalCap(ops, mode, self.clock)
        self.depeg = DepegMonitor(
            ops.stablecoin_depeg_threshold_pct, ops.stablecoin_pairs, alert_sink
        )
        self.clock_skew = ClockSkewMonitor(ops.max_clock_skew_ms, alert_sink)
        self.balance_cap = ExchangeBalanceCap(
            ops.max_balance_per_exchange_usdt,
            ops.balance_alert_repeat_hours,
            alert_sink,
            self.clock,
        )
        self.announcements: AnnouncementWatcher | None = None
        if ops.watch_exchange_announcements:
            if sources is None:
                sources = []
                if ops.announcements_file:
                    sources.append(FileAnnouncementSource(Path(ops.announcements_file)))
                if http_get is not None and "binance" in adapters and mode is not Mode.PAPER:
                    sources.append(BinanceAnnouncementSource(http_get))
            self.announcements = AnnouncementWatcher(
                sources,
                alert_sink,
                self.clock,
                maintenance_before=timedelta(minutes=ops.maintenance_block_before_minutes),
            )
        self.guard = OpsGuard(
            self.capital,
            LowLiquidityMode(ops.low_liquidity_mode),
            self.depeg,
            self.clock_skew,
            self.announcements,
        )
        self._last_announce: datetime | None = None
        self._last_check: datetime | None = None

    async def stable_prices(self) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        for pair in self.depeg.pairs:
            for ad in self.adapters.values():
                if pair not in ad.symbols():
                    continue
                px = await self._price(ad, pair)
                if px is not None:
                    prices[pair] = px
                    break
        return prices

    @staticmethod
    async def _price(ad: ExchangeAdapter, pair: str) -> Decimal | None:
        try:
            return (await ad.fetch_ticker(pair)).last
        except ExchangeAdapterError:
            pass
        try:  # e.g. paper without a live source: mid of the cached book
            book = await ad.fetch_order_book(pair, 5)
        except ExchangeAdapterError as exc:
            _log.debug("stable_price_failed", pair=pair, error=str(exc))
            return None
        if book.best_bid is None or book.best_ask is None:
            return None
        return (book.best_bid.price + book.best_ask.price) / 2

    async def maybe_check(self, per_exchange_equity: dict[str, Decimal] | None = None) -> bool:
        """Run `check` at most every `operations.check_interval_seconds`."""
        now = self.clock.now()
        every = self.cfg.operations.check_interval_seconds
        if self._last_check is not None and (now - self._last_check).total_seconds() < every:
            return False
        self._last_check = now
        await self.check(per_exchange_equity)
        return True

    async def check(self, per_exchange_equity: dict[str, Decimal] | None = None) -> None:
        now = self.clock.now()
        interval = self.cfg.operations.announcement_check_minutes * 60
        if self.announcements is not None and (
            self._last_announce is None or (now - self._last_announce).total_seconds() >= interval
        ):
            self._last_announce = now
            await self.announcements.refresh()
        await self.depeg.update(await self.stable_prices())
        await self.clock_skew.check(self.adapters)
        if per_exchange_equity:
            await self.balance_cap.check(per_exchange_equity)

    def positions_to_close(self, positions: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
        """(position_id, reason) for open positions in delisted pairs, if configured to close."""
        if self.announcements is None or self.cfg.operations.on_delisting != "close":
            return []
        out = []
        for pid, exchange, symbol in positions:
            reason = self.announcements.block_reason(exchange, symbol)
            if reason and reason.startswith("delisting"):
                out.append((pid, reason))
        return out
