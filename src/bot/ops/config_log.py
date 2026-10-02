"""Config change log (spec §5.13).

At start-up the effective config (YAML, never secrets) is flattened and diffed against the last
snapshot stored in the DB; each changed key becomes a `config_changes` row with the previous
value. Runtime changes (panel / Telegram / CLI: strategy toggles, kill switch, capital cap,
shadow approvals) are recorded through `record()`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from bot.config.schema import AppConfig
from bot.core.clock import Clock, SystemClock
from bot.storage.models import ConfigChangeRow
from bot.storage.repository import Repository

SNAPSHOT_KEY = "config_snapshot"
MAX_VALUE = 2000


def flatten(data: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(data, dict) and data:
        for k, v in data.items():
            out |= flatten(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        out[prefix] = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    return out


def snapshot(cfg: AppConfig) -> dict[str, str]:
    return flatten(json.loads(cfg.model_dump_json()))


def diff(old: dict[str, str], new: dict[str, str]) -> list[tuple[str, str | None, str | None]]:
    keys = sorted(set(old) | set(new))
    return [(k, old.get(k), new.get(k)) for k in keys if old.get(k) != new.get(k)]


class ConfigChangeLog:
    def __init__(self, repo: Repository, clock: Clock | None = None) -> None:
        self.repo = repo
        self.clock = clock or SystemClock()

    def _row(
        self, path: str, old: str | None, new: str | None, source: str, operator: str
    ) -> ConfigChangeRow:
        return ConfigChangeRow(
            path=path[:200],
            old_value=None if old is None else old[:MAX_VALUE],
            new_value=None if new is None else new[:MAX_VALUE],
            source=source,
            operator=operator[:64],
            timestamp=self.clock.now(),
        )

    async def sync_file_config(self, cfg: AppConfig) -> int:
        """Record differences against the previous run's config. Returns number of changes."""
        new = snapshot(cfg)
        stored = await self.repo.get_state(SNAPSHOT_KEY)
        if stored is None:
            await self.repo.save_config_changes(
                [self._row("*", None, f"{len(new)} ayar", "config_file", "ilk kayıt")]
            )
            await self.repo.set_state(SNAPSHOT_KEY, new)
            return 0
        changes = diff({k: str(v) for k, v in stored.items()}, new)
        await self.repo.save_config_changes(
            [self._row(p, o, n, "config_file", "") for p, o, n in changes]
        )
        await self.repo.set_state(SNAPSHOT_KEY, new)
        return len(changes)

    async def record(
        self, path: str, old: object, new: object, source: str, operator: str = ""
    ) -> None:
        def enc(v: object) -> str | None:
            return None if v is None else json.dumps(v, default=str, ensure_ascii=False)

        await self.repo.save_config_changes([self._row(path, enc(old), enc(new), source, operator)])

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.repo.config_changes(limit)
        return [
            {
                "timestamp": _iso(r.timestamp),
                "path": r.path,
                "old": r.old_value,
                "new": r.new_value,
                "source": r.source,
                "operator": r.operator,
            }
            for r in rows
        ]


def _iso(t: datetime) -> str:
    return t.isoformat()
