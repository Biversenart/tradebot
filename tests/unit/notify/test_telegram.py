from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from pydantic import SecretStr

from bot.config.schema import TelegramConfig
from bot.control import BotControl
from bot.core.clock import ManualClock
from bot.core.event_bus import EventBus
from bot.core.events import AlertLevel, PositionEvent, RiskAlert
from bot.notify.telegram import TelegramApi, TelegramBot
from tests.unit.api.conftest import control as control

TOKEN = SecretStr("123456:ABCDEF-secret")


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.updates: list[dict[str, object]] = []
        self.fail = False

    async def request_json(
        self, method: str, url: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, dict(payload or {})))
        if self.fail:
            return 500, {"ok": False}
        if name == "getUpdates":
            ups, self.updates = self.updates, []
            return 200, {"ok": True, "result": ups}
        return 200, {"ok": True, "result": {}}

    def sent(self) -> list[str]:
        return [str(p["text"]) for n, p in self.calls if n == "sendMessage"]


@pytest.fixture
async def bot(control: BotControl) -> AsyncIterator[tuple[TelegramBot, FakeHttp, EventBus]]:
    http = FakeHttp()
    bus = EventBus()
    b = TelegramBot(
        TelegramApi(TOKEN, http),
        "42",
        control,
        bus,
        TelegramConfig(enabled=True),
        ManualClock(datetime(2026, 1, 1, 23, tzinfo=UTC)),
    )
    yield b, http, bus


async def test_commands(bot: tuple[TelegramBot, FakeHttp, EventBus]) -> None:
    b, _, _ = bot
    assert "/analiz" in (await b.handle("/yardim", "42") or "")
    st = await b.handle("/durum", "42") or ""
    assert "Mod: paper" in st and "Açık pozisyon: 1" in st and "kapalı" in st
    pos = await b.handle("/pozisyonlar@mybot", "42") or ""
    assert "BTC/USDT LONG 0.5" in pos and "TP 1/2" in pos
    ana = await b.handle("/analiz eth", "42") or ""
    assert "ETH/USDT" in ana and "panelde" in ana
    assert "Kullanım" in (await b.handle("/analiz", "42") or "")
    assert "Bilinmeyen" in (await b.handle("/foo", "42") or "")


async def test_stop_and_resume(bot: tuple[TelegramBot, FakeHttp, EventBus]) -> None:
    b, _, _ = bot
    assert "durduruldu" in (await b.handle("/durdur", "42") or "")
    assert not b.control.kill_switch.allows_new_orders()
    assert "devam" in (await b.handle("/devam", "42") or "").lower()
    assert b.control.kill_switch.allows_new_orders()


async def test_unauthorized_chat_ignored(bot: tuple[TelegramBot, FakeHttp, EventBus]) -> None:
    b, _, _ = bot
    assert await b.handle("/durdur", "999") is None
    assert b.control.kill_switch.allows_new_orders()


async def test_polling_replies_only_to_owner(bot: tuple[TelegramBot, FakeHttp, EventBus]) -> None:
    b, http, _ = bot
    http.updates = [
        {"update_id": 5, "message": {"text": "/durum", "chat": {"id": 42}}},
        {"update_id": 6, "message": {"text": "/durdur", "chat": {"id": 7}}},
    ]
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(b.poll(stop, timeout=0), stopper())
    assert len(http.sent()) == 1 and "Mod: paper" in http.sent()[0]
    assert b._offset == 7
    assert b.control.kill_switch.allows_new_orders()


async def test_notifications(bot: tuple[TelegramBot, FakeHttp, EventBus]) -> None:
    b, http, _ = bot
    await b.on_alert(RiskAlert(level=AlertLevel.CRITICAL, code="kill_switch_manual", message="dur"))
    await b.on_alert(RiskAlert(level=AlertLevel.INFO, code="noise", message="skip"))
    await b.on_position(
        PositionEvent(
            kind="opened",
            position_id="p",
            exchange="x",
            symbol="BTC/USDT",
            side="long",
            amount=D(1),
            price=D(100),
            reason="breakout",
        )
    )
    await b.on_position(
        PositionEvent(
            kind="closed",
            position_id="p",
            exchange="x",
            symbol="BTC/USDT",
            side="long",
            amount=D(1),
            realized_pnl=D("-12.5"),
            reason="stop",
        )
    )
    sent = http.sent()
    assert len(sent) == 3
    assert sent[0].startswith("🛑") and "Pozisyon açıldı" in sent[1] and "-12.50" in sent[2]


async def test_daily_summary_and_failures(bot: tuple[TelegramBot, FakeHttp, EventBus]) -> None:
    b, http, _ = bot
    text = await b.daily_summary()
    assert "Günlük özet" in text and "10,050.00" in text
    http.fail = True
    assert await b.send("x") is False  # failures are logged, never raised
    assert all("ABCDEF-secret" not in str(p) for _, p in http.calls)


async def test_long_messages_truncated(bot: tuple[TelegramBot, FakeHttp, EventBus]) -> None:
    b, http, _ = bot
    await b.send("x" * 10_000)
    assert len(http.sent()[0]) <= 4000


def test_token_never_in_repr() -> None:
    assert "ABCDEF" not in repr(TelegramApi(TOKEN, FakeHttp()).__dict__)
