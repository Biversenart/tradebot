from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bot.main import app

runner = CliRunner()


def files(tmp_path: Path) -> list[str]:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "strategies:\n  ema_crossover: { enabled: true, fast: 9, slow: 21 }\n", encoding="utf-8"
    )
    env = tmp_path / ".env"
    env.write_text(f"DATABASE_URL=sqlite+aiosqlite:///{tmp_path / 'cli.db'}\n", encoding="utf-8")
    return ["-c", str(cfg), "--env-file", str(env)]


def test_capital_commands(tmp_path: Path) -> None:
    opts = files(tmp_path)
    r = runner.invoke(app, ["capital", "show", *opts])
    assert r.exit_code == 0 and "%10" in r.stdout
    r = runner.invoke(app, ["capital", "raise", "50", "--by", "ali", *opts])
    assert r.exit_code == 1 and "Kademeli" in r.output
    r = runner.invoke(app, ["capital", "raise", "20", "--by", "ali", *opts])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["capital", "show", *opts])
    assert "%20" in r.stdout and "ali" in r.stdout
    r = runner.invoke(app, ["capital", "raise", "abc", *opts])
    assert r.exit_code == 1


def test_shadow_and_config_log_commands(tmp_path: Path) -> None:
    opts = files(tmp_path)
    r = runner.invoke(app, ["shadow", "report", *opts])
    assert r.exit_code == 0 and "ema_crossover: onay yok" in r.stdout
    r = runner.invoke(app, ["shadow", "approve", "ema_crossover", "--by", "ali", *opts])
    assert r.exit_code == 0 and "onaylandı" in r.stdout
    assert "ema_crossover: onaylı" in runner.invoke(app, ["shadow", "report", *opts]).stdout
    assert runner.invoke(app, ["shadow", "approve", "yok", *opts]).exit_code == 1
    r = runner.invoke(app, ["config-log", *opts])
    assert r.exit_code == 0 and "shadow.approved.ema_crossover" in r.stdout
