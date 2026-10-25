"""CLI entry point (`bot ...`)."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Annotated

import typer

from bot import __version__
from bot.config import ConfigError, Settings, load_settings
from bot.core.events import AlertLevel, RiskAlert
from bot.log import configure_logging
from bot.runtime import BotRuntime

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
    typer.echo(f"Heartbeat: {'açık' if cfg.heartbeat.enabled else 'kapalı'}")
    typer.echo(f"Etkin borsalar: {', '.join(cfg.enabled_exchanges) or '-'}")
    typer.echo(f"Semboller: {', '.join(cfg.universe.symbols)}")
    typer.echo("Yapılandırma geçerli.")


async def _net_check(settings: Settings) -> int:
    rt = BotRuntime(settings)
    alerts: list[RiskAlert] = []

    async def collect(a: RiskAlert) -> None:
        alerts.append(a)

    rt.bus.subscribe(RiskAlert, collect)
    bus_task = asyncio.create_task(rt.bus.run())
    code = 0
    try:
        typer.echo(f"Egress modu: {rt.egress.mode}; proxy: {rt.egress.proxy or '-'}")
        if rt.ip_guard is not None:
            result = await rt.ip_guard.check()
            typer.echo(f"Dış IP durumu: {result.status}")
            for service, ip in result.observed.items():
                typer.echo(f"  {service}: {ip}")
            for service, err in result.errors.items():
                typer.echo(f"  {service}: HATA ({err})")
            if not result.ok:
                code = 1
        else:
            typer.echo("Paper/backtest modu: IP doğrulaması zorunlu değil (emir gönderilmez).")
        if code == 0:
            await rt.check_exchange_access()
    finally:
        await rt.http.close()
        await rt.bus.stop()
        await bus_task
    for a in alerts:
        typer.echo(f"[{a.level}] {a.message}")
        if a.level is AlertLevel.CRITICAL:
            code = 1
    typer.echo("Emirlere izin: " + ("EVET" if code == 0 else "HAYIR (fail-closed)"))
    return code


@app.command("net-check")
def net_check(config: ConfigOption = DEFAULT_CONFIG, env_file: EnvOption = DEFAULT_ENV) -> None:
    """Dış IP'yi egress üzerinden doğrula ve borsa erişimini test et (451/403 kontrolü)."""
    settings = _load_or_exit(config, env_file)
    raise typer.Exit(code=asyncio.run(_net_check(settings)))


async def run_bot(settings: Settings, stop: asyncio.Event) -> None:
    """Servisleri başlatır (EventBus, IP doğrulama, heartbeat), durdurma sinyalini bekler."""
    await BotRuntime(settings).run(stop)


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
