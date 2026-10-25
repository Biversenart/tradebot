"""Wires the long-running services together (EventBus, IP guard, heartbeat)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from bot import __version__
from bot.config import Mode, Settings
from bot.core.event_bus import EventBus
from bot.core.events import AlertLevel, RiskAlert
from bot.exchanges.base import ExchangeAdapter
from bot.exchanges.errors import ExchangeAdapterError
from bot.exchanges.factory import build_exchanges
from bot.log import get_logger
from bot.marketdata.feed import MarketDataFeed
from bot.net.egress import EgressHttpSession, resolve_egress
from bot.net.errors import EgressMisconfiguredError
from bot.net.exchange_access import check_exchange_access, ping_url
from bot.net.ip_guard import IpFetcher, IpGuard
from bot.notify.heartbeat import Heartbeat, Pinger

_log = get_logger("bot")


class BotRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        ip_fetcher: IpFetcher | None = None,
        heartbeat_pinger: Pinger | None = None,
    ) -> None:
        self.settings = settings
        self.bus = EventBus()
        cfg = settings.config
        self.egress = resolve_egress(settings)
        self.http: EgressHttpSession = self.egress.new_http_session()

        self.ip_guard: IpGuard | None = None
        if settings.sends_real_orders:
            expected_ip = settings.secrets.egress_expected_ip
            if expected_ip is None:  # loader enforces this; double-check, fail closed
                raise EgressMisconfiguredError("EGRESS_EXPECTED_IP zorunlu.")
            self.ip_guard = IpGuard(
                expected_ip=expected_ip,
                services=cfg.egress.ip_check_services,
                fetch_ip=ip_fetcher or self.http.get_text,
                check_interval=timedelta(minutes=cfg.egress.check_interval_minutes),
                alert_sink=self.bus.publish,
            )

        self.exchanges: dict[str, ExchangeAdapter] = {}
        self.feeds: list[MarketDataFeed] = []
        md = cfg.marketdata
        if md.enabled and settings.mode is not Mode.BACKTEST:
            self.exchanges = build_exchanges(settings, self.egress)
            for adapter in self.exchanges.values():
                self.feeds.append(
                    MarketDataFeed(
                        adapter,
                        self.bus,
                        cfg.universe.symbols,
                        md.candle_timeframes,
                        depth=md.order_book_depth,
                    )
                )

        self.heartbeat: Heartbeat | None = None
        url = settings.secrets.heartbeat_url
        if cfg.heartbeat.enabled and url is not None:
            self.heartbeat = Heartbeat(
                url=url,
                interval=timedelta(seconds=cfg.heartbeat.interval_seconds),
                ping=heartbeat_pinger or self._ping,
                is_healthy=self.is_healthy,
            )

    async def _ping(self, url: str) -> int:
        status, _ = await self.http.request_text("GET", url)
        return status

    def is_healthy(self) -> bool:
        return self.ip_guard is None or self.ip_guard.allows_orders()

    def orders_allowed(self) -> bool:
        """Gate consulted by the execution engine before any real order (fail-closed)."""
        if not self.settings.sends_real_orders:
            return True
        return self.ip_guard is not None and self.ip_guard.allows_orders()

    async def check_exchange_access(self) -> None:
        for name, ex in self.settings.config.enabled_exchanges.items():
            try:
                url = ping_url(name, ex.market_type, ex.testnet)
            except KeyError:
                continue
            result = await check_exchange_access(self.http, name, url)
            if result.ok:
                _log.info("exchange_access_ok", exchange=name)
                continue
            await self.bus.publish(
                RiskAlert(
                    level=AlertLevel.CRITICAL,
                    code=f"exchange_access_{result.kind}",
                    message=result.message,
                    details={"exchange": name, "status": result.status},
                )
            )

    async def _start_feeds(self, stop: asyncio.Event, tasks: list[asyncio.Task[None]]) -> None:
        if self.settings.sends_real_orders and not self.orders_allowed():
            # Unverified egress: do not contact exchanges at all (fail-closed).
            _log.warning("marketdata_not_started_egress_unverified")
            return
        for feed in self.feeds:
            try:
                await feed.adapter.load_markets()
            except ExchangeAdapterError as exc:
                await self.bus.publish(
                    RiskAlert(
                        level=AlertLevel.CRITICAL,
                        code=f"exchange_{exc.kind}",
                        message=f"{feed.adapter.name}: piyasa verisi başlatılamadı: {exc}",
                    )
                )
                continue
            tasks.append(asyncio.create_task(feed.run(stop), name=f"feed:{feed.adapter.name}"))

    async def run(self, stop: asyncio.Event) -> None:
        bus_task = asyncio.create_task(self.bus.run(), name="event-bus")
        tasks: list[asyncio.Task[None]] = []
        _log.info("bot_started", mode=str(self.settings.mode), version=__version__)
        try:
            if self.ip_guard is not None:
                await self.ip_guard.check()
                if self.ip_guard.allows_orders():
                    await self.check_exchange_access()
                tasks.append(asyncio.create_task(self.ip_guard.run_periodic(stop)))
            if self.heartbeat is not None:
                tasks.append(asyncio.create_task(self.heartbeat.run(stop)))
            if self.feeds:
                await self._start_feeds(stop, tasks)
            await stop.wait()
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ex in self.exchanges.values():
                with contextlib.suppress(Exception):
                    await ex.close()
            await self.http.close()
            await self.bus.stop()
            await bus_task
            _log.info("bot_stopped", dispatched=self.bus.dispatched)
