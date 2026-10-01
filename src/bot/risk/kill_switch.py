"""Kill switch (CLAUDE.md rule 5, spec §5.6). Always on; persists across restarts.

Triggers:
- DAILY_LOSS     day PnL <= -daily_loss_limit_pct  -> no new orders until next UTC day (auto)
- MAX_DRAWDOWN   drawdown >= max_drawdown_pct     -> stop + close positions (config); manual resume
- ERRORS         N consecutive API errors          -> pause; manual resume
- EGRESS_IP      external IP not verified          -> block; clears automatically when verified
- MANUAL         panel / Telegram                  -> stop (+ close per config); manual resume
- EXTERNAL       other safety nets (depeg, announcements, ...) -> block; manual resume
Exits (reduce-only orders) are always allowed so positions can be protected/closed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from bot.config.schema import RiskConfig
from bot.core.clock import Clock, SystemClock
from bot.core.events import AlertLevel, RiskAlert
from bot.net.errors import ErrorKind

AlertSink = Callable[[RiskAlert], Awaitable[None]]
ZERO = Decimal(0)


class KillReason(StrEnum):
    DAILY_LOSS = "daily_loss"
    MAX_DRAWDOWN = "max_drawdown"
    ERRORS = "consecutive_errors"
    EGRESS_IP = "egress_ip"
    MANUAL = "manual"
    EXTERNAL = "external"


AUTO_REASONS = {KillReason.DAILY_LOSS, KillReason.EGRESS_IP}
CLOSING_REASONS = {KillReason.MAX_DRAWDOWN, KillReason.MANUAL}


@dataclass
class KillSwitchState:
    reasons: dict[str, str] = field(default_factory=dict)
    paused_until: datetime | None = None
    close_requested: bool = False
    consecutive_errors: int = 0
    peak_equity: Decimal = ZERO
    day: str = ""
    day_start_equity: Decimal = ZERO
    triggered_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reasons": dict(self.reasons),
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "close_requested": self.close_requested,
            "consecutive_errors": self.consecutive_errors,
            "peak_equity": str(self.peak_equity),
            "day": self.day,
            "day_start_equity": str(self.day_start_equity),
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KillSwitchState:
        return cls(
            reasons=dict(d.get("reasons", {})),
            paused_until=datetime.fromisoformat(d["paused_until"])
            if d.get("paused_until")
            else None,
            close_requested=bool(d.get("close_requested", False)),
            consecutive_errors=int(d.get("consecutive_errors", 0)),
            peak_equity=Decimal(d.get("peak_equity", "0")),
            day=str(d.get("day", "")),
            day_start_equity=Decimal(d.get("day_start_equity", "0")),
            triggered_at=datetime.fromisoformat(d["triggered_at"])
            if d.get("triggered_at")
            else None,
        )


class KillSwitch:
    def __init__(
        self,
        risk: RiskConfig,
        clock: Clock | None = None,
        alert_sink: AlertSink | None = None,
        state: KillSwitchState | None = None,
    ) -> None:
        self.risk = risk
        self.clock = clock or SystemClock()
        self.alert_sink = alert_sink
        self.state = state or KillSwitchState()

    # ---------------------------------------------------------------- queries
    @property
    def active_reasons(self) -> list[KillReason]:
        self._expire_daily()
        return [KillReason(r) for r in self.state.reasons]

    @property
    def is_active(self) -> bool:
        return bool(self.active_reasons)

    def allows_new_orders(self) -> bool:
        return not self.is_active

    @property
    def close_positions_requested(self) -> bool:
        return self.state.close_requested and self.risk.on_kill_switch == "close_all"

    def _expire_daily(self) -> None:
        pu = self.state.paused_until
        if pu is not None and self.clock.now() >= pu:
            self.state.reasons.pop(KillReason.DAILY_LOSS.value, None)
            self.state.paused_until = None

    # ---------------------------------------------------------------- triggers
    async def trigger(self, reason: KillReason, message: str, close: bool | None = None) -> None:
        new = reason.value not in self.state.reasons
        self.state.reasons[reason.value] = message
        if close is None:
            close = reason in CLOSING_REASONS
        if close:
            self.state.close_requested = True
        if new:
            self.state.triggered_at = self.clock.now()
            await self._alert(
                AlertLevel.CRITICAL,
                f"kill_switch_{reason.value}",
                f"KILL SWITCH: {message} Yeni emirler durduruldu"
                + (" ve açık pozisyonlar kapatılacak." if self.close_positions_requested else "."),
            )

    async def record_equity(self, equity: Decimal) -> None:
        now = self.clock.now()
        day = now.astimezone(UTC).strftime("%Y-%m-%d")
        st = self.state
        if st.day != day:
            st.day, st.day_start_equity = day, equity
        st.peak_equity = max(st.peak_equity, equity)
        if st.day_start_equity > 0:
            day_pnl_pct = (equity - st.day_start_equity) / st.day_start_equity * 100
            if (
                day_pnl_pct <= -self.risk.daily_loss_limit_pct
                and KillReason.DAILY_LOSS.value not in st.reasons
            ):
                tomorrow = datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1)
                st.paused_until = tomorrow
                await self.trigger(
                    KillReason.DAILY_LOSS,
                    f"Günlük zarar %{-day_pnl_pct:.2f} (limit %{self.risk.daily_loss_limit_pct}); "
                    f"{tomorrow:%Y-%m-%d %H:%M} UTC'ye kadar yeni işlem yok.",
                    close=False,
                )
        if st.peak_equity > 0:
            dd = (st.peak_equity - equity) / st.peak_equity * 100
            if dd >= self.risk.max_drawdown_pct:
                await self.trigger(
                    KillReason.MAX_DRAWDOWN,
                    f"Drawdown %{dd:.2f} >= limit %{self.risk.max_drawdown_pct}.",
                )

    async def record_error(self, kind: ErrorKind, detail: str = "") -> None:
        if not kind.counts_for_kill_switch:
            return
        self.state.consecutive_errors += 1
        n = self.state.consecutive_errors
        if n >= self.risk.max_consecutive_errors:
            await self.trigger(
                KillReason.ERRORS,
                f"Art arda {n} API hatası (son: {kind}{': ' + detail if detail else ''}).",
                close=False,
            )

    def record_success(self) -> None:
        self.state.consecutive_errors = 0

    async def set_egress_ok(self, ok: bool, detail: str = "") -> None:
        if ok:
            if self.state.reasons.pop(KillReason.EGRESS_IP.value, None) is not None:
                await self._alert(
                    AlertLevel.INFO,
                    "kill_switch_egress_cleared",
                    "Dış IP doğrulandı; IP kaynaklı blok kaldırıldı.",
                )
        else:
            await self.trigger(
                KillReason.EGRESS_IP, f"Dış IP doğrulanamadı {detail}".strip() + ".", close=False
            )

    async def resume(self, operator: str = "manual") -> list[str]:
        """Manual reset of hard reasons (not the daily pause, not an unverified IP)."""
        cleared = [r for r in list(self.state.reasons) if KillReason(r) not in AUTO_REASONS]
        for r in cleared:
            self.state.reasons.pop(r, None)
        self.state.close_requested = False
        self.state.consecutive_errors = 0
        if KillReason.MAX_DRAWDOWN.value in cleared:
            self.state.peak_equity = ZERO  # restart drawdown measurement from current equity
        if cleared:
            await self._alert(
                AlertLevel.WARNING,
                "kill_switch_resumed",
                f"Kill switch {operator} tarafından kaldırıldı: {', '.join(cleared)}.",
            )
        return cleared

    async def _alert(self, level: AlertLevel, code: str, message: str) -> None:
        if self.alert_sink is not None:
            await self.alert_sink(
                RiskAlert(
                    level=level,
                    code=code,
                    message=message,
                    details={"reasons": list(self.state.reasons)},
                )
            )
