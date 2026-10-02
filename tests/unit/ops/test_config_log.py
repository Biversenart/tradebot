from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bot.config.schema import AppConfig
from bot.core.clock import ManualClock
from bot.ops.config_log import ConfigChangeLog, diff, flatten
from bot.storage.repository import Repository


def test_flatten_and_diff() -> None:
    flat = flatten({"a": {"b": 1, "c": [1, 2]}, "d": {}})
    assert flat == {"a.b": "1", "a.c": "[1, 2]", "d": "{}"}
    assert diff({"x": "1", "y": "2"}, {"x": "1", "y": "3", "z": "4"}) == [
        ("y", "2", "3"),
        ("z", None, "4"),
    ]


async def test_file_changes_recorded_with_previous_value(tmp_path: Path) -> None:
    repo = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    try:
        log = ConfigChangeLog(repo, ManualClock(datetime(2026, 1, 1, tzinfo=UTC)))
        assert await log.sync_file_config(AppConfig()) == 0  # first run: baseline
        assert await log.sync_file_config(AppConfig()) == 0
        changed = AppConfig.model_validate({"risk": {"risk_per_trade_pct": 0.5}})
        assert await log.sync_file_config(changed) == 1
        await log.record("runtime.kill_switch", False, True, "panel", "Panel (session)")
        rows = await log.recent()
        assert rows[0]["path"] == "runtime.kill_switch" and rows[0]["source"] == "panel"
        risk = next(r for r in rows if r["path"] == "risk.risk_per_trade_pct")
        assert risk["old"] == '"1"' and risk["new"] == '"0.5"'
        assert risk["source"] == "config_file"
        assert rows[-1]["path"] == "*"
    finally:
        await repo.close()


def test_snapshot_never_contains_secrets() -> None:
    flat = flatten(AppConfig().model_dump(mode="json"))
    assert not any("key" in k.lower() and "secret" in k.lower() for k in flat)
    assert not any("token" in k.lower() or "password" in k.lower() for k in flat)
