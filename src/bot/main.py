"""CLI entry point (`bot ...`)."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Annotated

import typer

from bot import __version__
from bot.config import ConfigError, Settings, load_settings
from bot.core.event_bus import EventBus
from bot.log import configure_logging, get_logger

app = typer.Typer(help="Kripto trade & arbitraj botu", no_args_is_help=True)

ConfigOption = Annotated[Path, typer.Option("--config", "-c", help="YAML config dosyası")]
EnvOption = Annotated[Path, typer.Option("--env-file", help=".env dosyası")]

DEFAULT_CONFIG = Path("config/config.yaml")
DEFAULT_ENV = Path(".env")


def _load_or_exit(config: Path, env_file: Path) -> Settings:
    try:
        return load_settings(config, env_file)
    except ConfigError as exc:
        typer.secho(f"Yapılandırma hatası: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Sürümü yazdır."""
    typer.echo(__version__)


@app.command("config-check")
def config_check(config: ConfigOption = DEFAULT_CONFIG, env_file: EnvOption = DEFAULT_ENV) -> None:
    """Config ve .env'i doğrula, etkin modu göster (sırlar yazdırılmaz)."""
    settings = _load_or_exit(config, env_file)
    cfg = settings.config
    typer.echo(f"Mod: {settings.mode}")
    typer.echo(f"Egress: {cfg.egress.mode} (fail-closed)")
    typer.echo(f"Etkin borsalar: {', '.join(cfg.enabled_exchanges) or '-'}")
    typer.echo(f"Semboller: {', '.join(cfg.universe.symbols)}")
    typer.echo("Yapılandırma geçerli.")


async def run_bot(settings: Settings, stop: asyncio.Event) -> None:
    """Aşama 0 çalışma döngüsü: EventBus'ı başlatır, durdurma sinyalini bekler."""
    log = get_logger("bot")
    bus = EventBus()
    bus_task = asyncio.create_task(bus.run(), name="event-bus")
    log.info("bot_started", mode=str(settings.mode), version=__version__)
    try:
        await stop.wait()
    finally:
        await bus.stop()
        await bus_task
        log.info("bot_stopped", dispatched=bus.dispatched)


@app.command()
def run(config: ConfigOption = DEFAULT_CONFIG, env_file: EnvOption = DEFAULT_ENV) -> None:
    """Botu başlat (Aşama 0: yalnızca iskelet; emir gönderilmez)."""
    settings = _load_or_exit(config, env_file)
    configure_logging(settings.config.log_level)

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_bot(settings, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    app()
