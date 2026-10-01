from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from bot.analysis.service import AnalysisOutput
from bot.config import Mode, Settings
from bot.config.schema import AppConfig
from bot.config.secrets import Secrets
from bot.control import BotControl
from bot.core.clock import ManualClock
from bot.risk.kill_switch import KillSwitch
from bot.storage.models import PositionRow
from bot.storage.repository import Repository
from bot.strategies.registry import build_strategy

T = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def control(tmp_path: Path) -> AsyncIterator[BotControl]:
    repo = await Repository.connect(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    await repo.save_position(
        PositionRow(
            position_id="p1",
            exchange="paper",
            symbol="BTC/USDT",
            side="long",
            strategy="breakout",
            amount=D("0.5"),
            initial_amount=D(1),
            entry_price=D(100),
            initial_stop=D(95),
            stop_loss=D(100),
            take_profits=["105", "110"],
            targets_hit=1,
            status="open",
            opened_at=T,
        )
    )
    await repo.save_equity(D(10_050))
    cfg = AppConfig()
    ks = KillSwitch(cfg.risk, ManualClock(T))
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "BTC-USDT_1.md").write_text(
        "# BTC/USDT — Teknik Analiz Raporu\n<script>x</script>", encoding="utf-8"
    )
    (reports / "BTC-USDT_1.html").write_text("<html>chart</html>", encoding="utf-8")
    (tmp_path / "secret.md").write_text("gizli", encoding="utf-8")

    async def analyze(symbol: str) -> AnalysisOutput:
        md = reports / "ETH-USDT_2.md"
        md.write_text(f"# {symbol} — Teknik Analiz Raporu\nözet satırı", encoding="utf-8")
        return AnalysisOutput(symbol, md.read_text("utf-8"), md, md.with_suffix(".html"), None)

    ctl = BotControl(
        Settings(cfg, Secrets(_env_file=None), Mode.PAPER),
        repo,
        ks,
        [build_strategy(cfg, "breakout", "BTC/USDT")],
        lambda: D(10_050),
        analyze=analyze,
        reports_dir=reports,
    )
    yield ctl
    await repo.close()
