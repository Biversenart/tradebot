from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from bot import __version__
from bot.config import Mode, load_settings
from bot.main import app, run_bot

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_check_example(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "config-check",
            "-c",
            str(ROOT / "config/config.example.yaml"),
            "--env-file",
            str(tmp_path / "none.env"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Mod: paper" in result.stdout


def test_config_check_rejects_unconfirmed_live(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("mode: live\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("TRADING_MODE=live\n", encoding="utf-8")
    result = runner.invoke(app, ["config-check", "-c", str(cfg), "--env-file", str(env)])
    assert result.exit_code == 2
    assert "live_trading_confirmed" in result.output


async def test_run_bot_stops_cleanly(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "yok.yaml", None)
    assert settings.mode is Mode.PAPER
    stop = asyncio.Event()
    task = asyncio.create_task(run_bot(settings, stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
