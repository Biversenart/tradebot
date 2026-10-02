"""BotControl: one facade for Telegram commands and the web panel (no direct order access)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot import __version__
from bot.analysis.service import AnalysisOutput
from bot.config import Settings
from bot.ops.capital import CapitalCapError
from bot.ops.config_log import ConfigChangeLog
from bot.ops.service import OpsService
from bot.ops.shadow import ShadowBook, ShadowRegistry
from bot.risk.kill_switch import KillReason, KillSwitch
from bot.storage.repository import Repository
from bot.strategies.base import BaseStrategy

AnalyzeFn = Callable[[str], Awaitable[AnalysisOutput]]
ZERO = Decimal(0)


@dataclass
class StatusSnapshot:
    mode: str
    version: str
    equity: Decimal
    reserve: Decimal
    kill_switch_active: bool
    kill_reasons: dict[str, str]
    egress_ok: bool
    open_positions: int
    strategies: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "version": self.version,
            "equity": str(self.equity),
            "reserve": str(self.reserve),
            "kill_switch_active": self.kill_switch_active,
            "kill_reasons": self.kill_reasons,
            "egress_ok": self.egress_ok,
            "open_positions": self.open_positions,
            "strategies": self.strategies,
        }


class BotControl:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        kill_switch: KillSwitch,
        strategies: list[BaseStrategy],
        equity: Callable[[], Decimal],
        reserve: Callable[[], Decimal] = lambda: ZERO,
        egress_ok: Callable[[], bool] = lambda: True,
        analyze: AnalyzeFn | None = None,
        reports_dir: Path = Path("reports/analysis"),
        persist: Callable[[], Awaitable[None]] | None = None,
        config_log: ConfigChangeLog | None = None,
        ops: OpsService | None = None,
        shadow: tuple[ShadowRegistry, ShadowBook] | None = None,
        persist_ops: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.kill_switch = kill_switch
        self.strategies = strategies
        self._equity = equity
        self._reserve = reserve
        self._egress_ok = egress_ok
        self._analyze = analyze
        self.reports_dir = reports_dir
        self._persist = persist
        self.config_log = config_log
        self.ops = ops
        self.shadow = shadow
        self._persist_ops = persist_ops
        self.disabled: set[str] = set()

    async def _log_change(self, path: str, old: object, new: object, operator: str) -> None:
        if self.config_log is None:
            return
        low = operator.lower()
        source = "panel" if low.startswith("panel") else "telegram" if "telegram" in low else "cli"
        await self.config_log.record(path, old, new, source, operator)

    # ---------------------------------------------------------------- queries
    async def status(self) -> StatusSnapshot:
        return StatusSnapshot(
            mode=str(self.settings.mode),
            version=__version__,
            equity=self._equity(),
            reserve=self._reserve(),
            kill_switch_active=self.kill_switch.is_active,
            kill_reasons=dict(self.kill_switch.state.reasons),
            egress_ok=self._egress_ok(),
            open_positions=len(await self.repo.open_positions()),
            strategies={
                f"{s.name}:{s.symbol}": f"{s.name}:{s.symbol}" not in self.disabled
                for s in self.strategies
            },
        )

    async def positions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.position_id,
                "exchange": p.exchange,
                "symbol": p.symbol,
                "side": p.side,
                "amount": str(p.amount),
                "entry": str(p.entry_price),
                "stop": str(p.stop_loss),
                "targets": p.take_profits,
                "targets_hit": p.targets_hit,
                "strategy": p.strategy,
                "realized_pnl": str(p.realized_pnl),
                "opened_at": p.opened_at.isoformat(),
            }
            for p in await self.repo.open_positions()
        ]

    async def recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "id": p.position_id,
                "symbol": p.symbol,
                "side": p.side,
                "strategy": p.strategy,
                "entry": str(p.entry_price),
                "pnl": str(p.realized_pnl),
                "reason": p.close_reason,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            }
            for p in await self.repo.closed_positions(limit)
        ]

    async def equity_curve(self, limit: int = 2000) -> list[dict[str, str]]:
        return [
            {"t": e.timestamp.isoformat(), "equity": str(e.equity)}
            for e in await self.repo.equity_curve(limit)
        ]

    async def events(self, limit: int = 30) -> list[dict[str, Any]]:
        return [
            {"t": e.timestamp.isoformat(), "level": e.level, "code": e.code, "message": e.message}
            for e in await self.repo.recent_risk_events(limit)
        ]

    def reports(self) -> list[Path]:
        if not self.reports_dir.exists():
            return []
        return sorted(self.reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)

    def report_file(self, name: str) -> Path | None:
        """Resolve a report file name safely (no path traversal)."""
        if "/" in name or "\\" in name or name.startswith("."):
            return None
        path = (self.reports_dir / name).resolve()
        root = self.reports_dir.resolve()
        if path.parent != root or not path.is_file() or path.suffix not in (".md", ".html"):
            return None
        return path

    # ---------------------------------------------------------------- actions
    async def stop(self, operator: str) -> str:
        await self.kill_switch.trigger(KillReason.MANUAL, f"{operator} tarafından durduruldu.")
        await self._log_change("runtime.kill_switch", False, True, operator)
        if self._persist:
            await self._persist()
        close = self.kill_switch.close_positions_requested
        return "Bot durduruldu: yeni emir yok" + (
            ", açık pozisyonlar kapatılıyor." if close else "."
        )

    async def resume(self, operator: str) -> str:
        cleared = await self.kill_switch.resume(operator)
        if cleared:
            await self._log_change("runtime.kill_switch", True, False, operator)
        if self._persist:
            await self._persist()
        remaining = [r.value for r in self.kill_switch.active_reasons]
        if remaining:
            return (
                f"Kaldırılan: {', '.join(cleared) or '-'}. Hâlâ aktif (otomatik kalkar): "
                f"{', '.join(remaining)}."
            )
        return "Bot devam ediyor." if cleared else "Kill switch zaten aktif değildi."

    async def set_strategy_enabled(self, key: str, enabled: bool, operator: str = "cli") -> bool:
        keys = {f"{s.name}:{s.symbol}" for s in self.strategies}
        if key not in keys:
            return False
        before = key not in self.disabled
        if before != enabled:
            await self._log_change(f"runtime.strategies.{key}.enabled", before, enabled, operator)
        if enabled:
            self.disabled.discard(key)
        else:
            self.disabled.add(key)
        return True

    def strategy_allowed(self, strategy: BaseStrategy) -> bool:
        return f"{strategy.name}:{strategy.symbol}" not in self.disabled

    # ---------------------------------------------------------------- ops (spec §5.13)
    async def config_changes(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.config_log.recent(limit) if self.config_log else []

    def ops_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.ops is not None:
            o = self.ops
            out["capital_cap"] = o.capital.summary_tr()
            out["depeg"] = o.depeg.block_reason() or "normal"
            out["clock_offsets_ms"] = {k: round(v) for k, v in o.clock_skew.offsets.items()}
            out["clock_blocked"] = sorted(o.clock_skew.skewed)
            if o.announcements is not None:
                out["delisted"] = {
                    ex: sorted(o.announcements.delisted_assets(ex)) for ex in o.adapters
                }
        if self.shadow is not None:
            _, book = self.shadow
            n = self.settings.config.operations.shadow_min_trades
            out["shadow"] = [book.compare_tr(name, n) for name in sorted(self.shadow_names())]
        return out

    def shadow_names(self) -> set[str]:
        """Strategies with virtual trades in the shadow book (live and/or shadow version)."""
        if self.shadow is None:
            return set()
        _, book = self.shadow
        return {t.strategy for t in book.closed} | {k[1] for k in book.open}

    async def raise_capital_cap(self, pct: Decimal, operator: str) -> str:
        if self.ops is None:
            return "Ops servisi yok."
        old = self.ops.capital.state.cap_pct
        try:
            msg = self.ops.capital.set_cap(pct, operator)
        except CapitalCapError as exc:
            return f"Reddedildi: {exc}"
        await self._log_change("operations.live_capital_cap_pct", str(old), str(pct), operator)
        if self._persist_ops:
            await self._persist_ops()
        return msg

    async def approve_shadow(self, name: str, operator: str) -> str:
        if self.shadow is None:
            return "Gölge mod kayıt defteri yok."
        registry, _ = self.shadow
        try:
            msg = registry.approve(
                name, self.settings.config, operator, self.kill_switch.clock.now()
            )
        except KeyError:
            return f"Strateji bulunamadı: {name}"
        await self._log_change(f"shadow.approved.{name}", None, "approved", operator)
        if self._persist_ops:
            await self._persist_ops()
        return msg

    async def analyze(self, symbol: str) -> AnalysisOutput:
        if self._analyze is None:
            raise RuntimeError("Analiz servisi yapılandırılmadı.")
        return await self._analyze(symbol.upper())
