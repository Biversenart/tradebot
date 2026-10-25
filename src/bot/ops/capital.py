"""Canary capital cap (spec §5.13): live sizing sees only `live_capital_cap_pct` of equity.

The cap is persisted. Raising it is manual (`bot capital raise` / panel) and only allowed after
`canary_days` since live start or the previous raise, by at most `max_cap_step_factor` at once.
Lowering is always allowed. A config value lower than the persisted cap wins (safer); a higher
config value is ignored with a warning (raising must go through the approval path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from bot.config.schema import Mode, OperationsConfig
from bot.core.clock import Clock, SystemClock
from bot.log import get_logger

HUNDRED = Decimal(100)
STATE_KEY = "capital_cap"
_log = get_logger(__name__)


class CapitalCapError(ValueError):
    pass


@dataclass
class CapitalCapState:
    cap_pct: Decimal
    started_at: datetime | None = None
    last_change_at: datetime | None = None
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cap_pct": str(self.cap_pct),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_change_at": self.last_change_at.isoformat() if self.last_change_at else None,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CapitalCapState:
        def ts(v: Any) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        return cls(
            cap_pct=Decimal(d["cap_pct"]),
            started_at=ts(d.get("started_at")),
            last_change_at=ts(d.get("last_change_at")),
            history=list(d.get("history", [])),
        )


class CapitalCap:
    def __init__(self, cfg: OperationsConfig, mode: Mode, clock: Clock | None = None) -> None:
        self.cfg = cfg
        self.active = mode is Mode.LIVE
        self.clock = clock or SystemClock()
        self.state = CapitalCapState(cfg.live_capital_cap_pct)

    def restore(self, data: dict[str, Any] | None) -> None:
        if data:
            self.state = CapitalCapState.from_dict(data)
        cfg_pct = self.cfg.live_capital_cap_pct
        if cfg_pct < self.state.cap_pct:
            self.state.cap_pct = cfg_pct
        elif cfg_pct > self.state.cap_pct:
            _log.warning(
                "capital_cap_config_ignored",
                config=str(cfg_pct),
                active=str(self.state.cap_pct),
                hint="Artış için 'bot capital raise' (manuel onay) kullanın.",
            )
        if self.active and self.state.started_at is None:
            self.state.started_at = self.clock.now()

    @property
    def cap_pct(self) -> Decimal:
        return min(self.state.cap_pct, HUNDRED)

    def effective_equity(self, equity: Decimal) -> Decimal:
        if not self.active:
            return equity
        return equity * self.cap_pct / HUNDRED

    def next_raise_at(self) -> datetime | None:
        ref = self.state.last_change_at or self.state.started_at
        return ref + timedelta(days=self.cfg.canary_days) if ref else None

    def set_cap(self, new_pct: Decimal, operator: str) -> str:
        if new_pct <= 0 or new_pct > HUNDRED:
            raise CapitalCapError("Tavan 0 < x <= 100 olmalı.")
        old = self.state.cap_pct
        now = self.clock.now()
        if new_pct > old:
            due = self.next_raise_at()
            if due is not None and now < due:
                raise CapitalCapError(
                    f"Kanarya süresi dolmadı: artış en erken {due:%Y-%m-%d %H:%M} UTC."
                )
            if new_pct > old * self.cfg.max_cap_step_factor:
                raise CapitalCapError(
                    f"Kademeli artış: tek seferde en fazla x{self.cfg.max_cap_step_factor} "
                    f"(%{old} -> en çok %{old * self.cfg.max_cap_step_factor})."
                )
        self.state.cap_pct = new_pct
        self.state.last_change_at = now
        self.state.history.append(
            {"at": now.isoformat(), "old": str(old), "new": str(new_pct), "by": operator}
        )
        return f"Sermaye tavanı %{old} -> %{new_pct} ({operator})."

    def summary_tr(self) -> str:
        if not self.active:
            return "Kanarya tavanı yalnızca live modda uygulanır."
        due = self.next_raise_at()
        tail = f"; sonraki artış en erken {due:%Y-%m-%d} UTC" if due else ""
        return f"Live sermaye tavanı: %{self.cap_pct}{tail}."
