"""Offline ops actions for the CLI (DB state). While the bot runs, use the panel / Telegram:
the running bot persists its own copy of this state and would overwrite CLI edits."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from bot.config import Settings
from bot.ops import capital as capital_mod
from bot.ops import shadow as shadow_mod
from bot.ops.capital import CapitalCap
from bot.ops.config_log import ConfigChangeLog
from bot.ops.shadow import ShadowBook, ShadowRegistry
from bot.storage.repository import Repository


async def _repo(settings: Settings) -> Repository:
    return await Repository.connect(settings.secrets.database_url.get_secret_value())


async def capital_show(settings: Settings) -> str:
    repo = await _repo(settings)
    try:
        cap = CapitalCap(settings.config.operations, settings.mode)
        cap.restore(await repo.get_state(capital_mod.STATE_KEY))
        cap.active = True  # show the live figure regardless of current mode
        hist = cap.state.history[-5:]
        lines = [cap.summary_tr()] + [
            f"  {h['at']}: %{h['old']} -> %{h['new']} ({h['by']})" for h in hist
        ]
        return "\n".join(lines)
    finally:
        await repo.close()


async def capital_raise(settings: Settings, pct: Decimal, operator: str) -> str:
    repo = await _repo(settings)
    try:
        cap = CapitalCap(settings.config.operations, settings.mode)
        cap.restore(await repo.get_state(capital_mod.STATE_KEY))
        old = cap.state.cap_pct
        msg = cap.set_cap(pct, operator)
        await repo.set_state(capital_mod.STATE_KEY, cap.state.to_dict())
        await ConfigChangeLog(repo).record(
            "operations.live_capital_cap_pct", str(old), str(pct), "cli", operator
        )
        return msg
    finally:
        await repo.close()


async def shadow_report(settings: Settings) -> str:
    repo = await _repo(settings)
    try:
        reg = ShadowRegistry.from_dict(await repo.get_state(shadow_mod.STATE_KEY))
        book = ShadowBook.from_dict(await repo.get_state(shadow_mod.BOOK_KEY))
        n = settings.config.operations.shadow_min_trades
        lines = []
        for name, sc in settings.config.strategies.items():
            appr = reg.approved.get(name)
            cur = shadow_mod.fingerprint(shadow_mod.params_of(sc))
            state = (
                "onay yok (yeni)"
                if appr is None
                else ("onaylı" if shadow_mod.fingerprint(appr) == cur else "DEĞİŞTİ -> gölgede")
            )
            lines.append(f"{name}: {state} [{cur}]")
            if appr is None or shadow_mod.fingerprint(appr) != cur:
                lines.append("  " + book.compare_tr(name, n))
        return "\n".join(lines) or "Strateji yok."
    finally:
        await repo.close()


async def shadow_approve(settings: Settings, name: str, operator: str) -> str:
    repo = await _repo(settings)
    try:
        reg = ShadowRegistry.from_dict(await repo.get_state(shadow_mod.STATE_KEY))
        msg = reg.approve(name, settings.config, operator, datetime.now(UTC))
        await repo.set_state(shadow_mod.STATE_KEY, reg.to_dict())
        await ConfigChangeLog(repo).record(
            f"shadow.approved.{name}", None, "approved", "cli", operator
        )
        return msg
    finally:
        await repo.close()


async def config_log(settings: Settings, limit: int) -> str:
    repo = await _repo(settings)
    try:
        rows = await ConfigChangeLog(repo).recent(limit)
        return (
            "\n".join(
                f"{r['timestamp']} [{r['source']}] {r['path']}: {r['old']} -> {r['new']} "
                f"{r['operator']}"
                for r in rows
            )
            or "Kayıt yok."
        )
    finally:
        await repo.close()
