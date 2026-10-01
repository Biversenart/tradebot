"""Telegram notifications + commands (spec §5.10).

Commands (only from the configured TELEGRAM_CHAT_ID; everything else is ignored):
  /durum  /pozisyonlar  /analiz <coin>  /durdur  /devam  /yardim
Notifications: RiskAlert (warning/critical), position opened/reduced/closed, daily summary.
The token lives in the URL path, so it is never logged; errors never include the URL.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from pydantic import SecretStr

from bot.config.schema import TelegramConfig
from bot.control import BotControl
from bot.core.aio import wait_or_stop
from bot.core.clock import Clock, SystemClock
from bot.core.event_bus import EventBus
from bot.core.events import AlertLevel, PositionEvent, RiskAlert
from bot.log import get_logger
from bot.net.errors import EgressError

API = "https://api.telegram.org"
MAX_LEN = 4000
_log = get_logger(__name__)

HELP = (
    "Komutlar:\n"
    "/durum — mod, bakiye, kill switch, açık pozisyon sayısı\n"
    "/pozisyonlar — açık pozisyonlar\n"
    "/analiz <coin> — ör. /analiz BTC (MTF teknik analiz özeti)\n"
    "/durdur — kill switch: yeni emirleri durdur (config'e göre pozisyonları kapat)\n"
    "/devam — kill switch'i kaldır (günlük zarar ve IP blokları otomatik kalkar)\n"
    "/yardim — bu mesaj"
)


class JsonHttp(Protocol):
    async def request_json(
        self, method: str, url: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]: ...


class TelegramApi:
    def __init__(self, token: SecretStr, http: JsonHttp) -> None:
        self._token = token
        self._http = http

    async def call(self, method: str, **params: object) -> dict[str, object]:
        url = f"{API}/bot{self._token.get_secret_value()}/{method}"
        status, data = await self._http.request_json("POST", url, dict(params))
        if status >= 400 or not data.get("ok", False):
            raise EgressError(f"Telegram {method} başarısız (HTTP {status}).")
        return data


def _fmt_money(v: Any) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}"
    except Exception:
        return str(v)


class TelegramBot:
    def __init__(
        self,
        api: TelegramApi,
        chat_id: str,
        control: BotControl,
        bus: EventBus,
        cfg: TelegramConfig,
        clock: Clock | None = None,
        base_currency: str = "USDT",
    ) -> None:
        self.api = api
        self.chat_id = str(chat_id)
        self.control = control
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.base_currency = base_currency
        self.sent: list[str] = []
        self._offset = 0
        bus.subscribe(RiskAlert, self.on_alert)
        bus.subscribe(PositionEvent, self.on_position)

    # ---------------------------------------------------------------- outbound
    async def send(self, text: str) -> bool:
        text = text if len(text) <= MAX_LEN else text[: MAX_LEN - 20] + "\n…(kısaltıldı)"
        try:
            await self.api.call(
                "sendMessage", chat_id=self.chat_id, text=text, disable_web_page_preview=True
            )
        except EgressError as exc:
            _log.warning("telegram_send_failed", error=str(exc))
            return False
        self.sent.append(text)
        self.sent = self.sent[-100:]
        return True

    async def on_alert(self, alert: RiskAlert) -> None:
        if alert.level is AlertLevel.INFO and not alert.code.endswith("recovered"):
            return
        icon = {"critical": "🛑", "warning": "⚠️", "info": "ℹ️"}[alert.level.value]
        await self.send(f"{icon} {alert.message}\n({alert.code})")

    async def on_position(self, ev: PositionEvent) -> None:
        if ev.kind == "opened":
            msg = (
                f"🟢 Pozisyon açıldı: {ev.symbol} {ev.side.upper()} {ev.amount} @ {ev.price}"
                f" ({ev.reason})"
            )
        elif ev.kind == "reduced":
            msg = f"🟡 Kısmi çıkış: {ev.symbol} kalan {ev.amount} ({ev.reason})"
        else:
            sign = "✅" if ev.realized_pnl >= 0 else "🔴"
            msg = (
                f"{sign} Pozisyon kapandı: {ev.symbol} ({ev.reason}) PnL "
                f"{_fmt_money(ev.realized_pnl)} {self.base_currency}"
            )
        await self.send(msg)

    # ---------------------------------------------------------------- commands
    def _symbol(self, arg: str) -> str:
        arg = arg.strip().upper()
        return arg if "/" in arg else f"{arg}/{self.base_currency}"

    async def handle(self, text: str, chat_id: str) -> str | None:
        if str(chat_id) != self.chat_id:
            _log.warning("telegram_unauthorized_chat")
            return None
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return None
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("/start", "/yardim", "/help"):
            return HELP
        if cmd == "/durum":
            st = await self.control.status()
            ks = (
                ("AKTİF: " + "; ".join(st.kill_reasons.values()))
                if st.kill_switch_active
                else "kapalı"
            )
            return (
                f"Mod: {st.mode} (v{st.version})\nBakiye: {_fmt_money(st.equity)} "
                f"{self.base_currency} (rezerv {_fmt_money(st.reserve)})\n"
                f"Kill switch: {ks}\nEgress/IP: {'tamam' if st.egress_ok else 'DOĞRULANMADI'}\n"
                f"Açık pozisyon: {st.open_positions}"
            )
        if cmd == "/pozisyonlar":
            pos = await self.control.positions()
            if not pos:
                return "Açık pozisyon yok."
            return "\n".join(
                f"{p['symbol']} {p['side'].upper()} {p['amount']} @ {p['entry']} · stop {p['stop']}"
                f" · TP {p['targets_hit']}/{len(p['targets'])} · {p['strategy']}"
                for p in pos
            )
        if cmd == "/analiz":
            if not arg:
                return "Kullanım: /analiz BTC"
            try:
                out = await self.control.analyze(self._symbol(arg))
            except Exception as exc:
                return f"Analiz yapılamadı: {exc}"
            return out.short_tr(14) + f"\n\nTam rapor ve grafik panelde: {out.md_path.name}"
        if cmd == "/durdur":
            return await self.control.stop("Telegram")
        if cmd == "/devam":
            return await self.control.resume("Telegram")
        return "Bilinmeyen komut. /yardim"

    async def poll(self, stop: asyncio.Event, timeout: int = 25) -> None:
        while not stop.is_set():
            try:
                data = await self.api.call("getUpdates", offset=self._offset, timeout=timeout)
            except EgressError as exc:
                _log.warning("telegram_poll_failed", error=str(exc))
                await wait_or_stop(stop, 5)
                continue
            result = data.get("result")
            for upd in result if isinstance(result, list) else []:
                self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                msg = upd.get("message") or {}
                text, chat = msg.get("text"), (msg.get("chat") or {}).get("id")
                if not text or chat is None:
                    continue
                reply = await self.handle(str(text), str(chat))
                if reply:
                    await self.send(reply)
            await asyncio.sleep(0)  # always yield (long-poll may return instantly)

    # ---------------------------------------------------------------- daily summary
    async def daily_summary(self) -> str:
        st = await self.control.status()
        trades = await self.control.recent_trades(50)
        since = self.clock.now() - timedelta(days=1)
        today = [
            t for t in trades if t["closed_at"] and datetime.fromisoformat(t["closed_at"]) >= since
        ]
        pnl = sum((Decimal(t["pnl"]) for t in today), Decimal(0))
        wins = sum(1 for t in today if Decimal(t["pnl"]) > 0)
        return (
            f"📊 Günlük özet\nBakiye: {_fmt_money(st.equity)} {self.base_currency}\n"
            f"Son 24 saat: {len(today)} işlem, {wins} kazançlı, PnL {_fmt_money(pnl)}\n"
            f"Açık pozisyon: {st.open_positions}\n"
            f"Kill switch: {'AKTİF' if st.kill_switch_active else 'kapalı'}"
        )

    async def summary_loop(self, stop: asyncio.Event) -> None:
        last_day = ""
        while not stop.is_set():
            now = self.clock.now()
            day = now.strftime("%Y-%m-%d")
            if now.hour == self.cfg.daily_summary_hour and day != last_day:
                last_day = day
                await self.send(await self.daily_summary())
            await wait_or_stop(stop, 60)
