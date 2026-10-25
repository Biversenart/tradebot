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
    cfg = tmp_path / "c.yaml"
    cfg.write_text("marketdata: {enabled: false}\n", encoding="utf-8")
    settings = load_settings(cfg, None)
    assert settings.mode is Mode.PAPER
    stop = asyncio.Event()
    task = asyncio.create_task(run_bot(settings, stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def test_net_check_paper_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("exchanges: {binance: {enabled: false}}\n", encoding="utf-8")
    result = runner.invoke(
        app, ["net-check", "-c", str(cfg), "--env-file", str(tmp_path / "none.env")]
    )
    assert result.exit_code == 0, result.output
    assert "Paper" in result.stdout


def test_net_check_testnet_blocks_without_proxy(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "mode: testnet\negress:\n  mode: proxy\n  request_timeout_seconds: 2\n", encoding="utf-8"
    )
    env = tmp_path / ".env"
    # proxy points at a closed local port: must fail closed, never go direct
    env.write_text(
        "TRADING_MODE=testnet\nEGRESS_EXPECTED_IP=8.8.8.8\n"
        "EGRESS_PROXY_URL=socks5://u:topsecret@127.0.0.1:9\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["net-check", "-c", str(cfg), "--env-file", str(env)])
    assert result.exit_code == 1
    assert "unreachable" in result.stdout
    assert "HAYIR" in result.stdout
    assert "topsecret" not in result.output
