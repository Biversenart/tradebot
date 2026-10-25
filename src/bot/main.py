"""CLI entry point (`bot ...`)."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from bot import __version__
from bot.analysis.chart import build_figure, write_chart
from bot.analysis.data import load_frames
from bot.analysis.engine import analyze_symbol
from bot.analysis.plan import best_plan
from bot.analysis.report import render_report
from bot.config import ConfigError, Settings, load_settings
from bot.config.schema import ExchangeConfig
from bot.core.events import AlertLevel, RiskAlert
from bot.core.timeframes import timeframe_seconds
from bot.exchanges.errors import ExchangeAdapterError
from bot.exchanges.factory import build_public_adapter
from bot.log import configure_logging
from bot.marketdata.history import download_to_parquet, safe_symbol, utc_date
from bot.net.egress import resolve_egress
from bot.runtime import BotRuntime

app = typer.Typer(help="Kripto trade & arbitraj botu", no_args_is_help=True)
data_app = typer.Typer(help="Geçmiş veri komutları", no_args_is_help=True)
app.add_typer(data_app, name="data")

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


async def _download(
    settings: Settings,
    exchange: str,
    symbols: list[str],
    timeframes: list[str],
    since: datetime,
    until: datetime | None,
    out: Path,
) -> None:
    cfg = settings.config.exchanges.get(exchange) or ExchangeConfig(enabled=True)
    # Historical data always comes from the real public API (no keys), through the egress.
    adapter = build_public_adapter(exchange, cfg, resolve_egress(settings), testnet=False)
    async with adapter:
        for symbol in symbols:
            for tf in timeframes:
                path, n = await download_to_parquet(adapter, symbol, tf, since, out, until)
                typer.echo(f"{symbol} {tf}: {n} yeni mum -> {path}")


@data_app.command("download")
def data_download(
    symbol: Annotated[
        list[str], typer.Option("--symbol", "-s", help="ör. BTC/USDT (tekrarlanabilir)")
    ],
    since: Annotated[str, typer.Option("--since", help="Başlangıç (YYYY-MM-DD, UTC)")],
    tf: Annotated[list[str], typer.Option("--tf", help="Zaman dilimi (tekrarlanabilir)")] = ["1h"],  # noqa: B006
    until: Annotated[str | None, typer.Option("--until", help="Bitiş (YYYY-MM-DD, UTC)")] = None,
    exchange: Annotated[str, typer.Option("--exchange", "-e")] = "binance",
    out: Annotated[Path, typer.Option("--out", help="Veri kök klasörü")] = Path("data"),
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Geçmiş OHLCV verisini indirip Parquet'e yaz (kaldığı yerden devam eder)."""
    settings = _load_or_exit(config, env_file)
    configure_logging(settings.config.log_level)
    for t in tf:
        timeframe_seconds(t)  # validate early
    try:
        asyncio.run(
            _download(
                settings,
                exchange,
                symbol,
                tf,
                utc_date(since),
                utc_date(until) if until else None,
                out,
            )
        )
    except ExchangeAdapterError as exc:
        typer.secho(f"İndirme başarısız ({exc.kind}): {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


async def _analyze(
    settings: Settings,
    symbol: str,
    exchange: str,
    data_root: Path,
    out_dir: Path,
    offline: bool,
) -> int:
    cfg = settings.config.analysis
    tfs = [*cfg.timeframes.long, *cfg.timeframes.mid, *cfg.timeframes.short]
    adapter = None
    if not offline:
        ex_cfg = settings.config.exchanges.get(exchange) or ExchangeConfig(enabled=True)
        adapter = build_public_adapter(exchange, ex_cfg, resolve_egress(settings), testnet=False)
    try:
        if adapter is not None:
            await adapter.load_markets()
        frames, sources = await load_frames(
            symbol,
            tfs,
            exchange=exchange,
            data_root=data_root,
            bars=cfg.analysis_bars,
            adapter=adapter,
        )
    finally:
        if adapter is not None:
            await adapter.close()
    if not frames:
        typer.secho(
            f"{symbol} için veri yok. Önce: bot data download --symbol {symbol} --since ...",
            fg=typer.colors.RED,
            err=True,
        )
        return 1
    mtf = analyze_symbol(frames, symbol, exchange, cfg)
    if mtf.setup_timeframe() is None:
        typer.secho("Analiz için yeterli mum yok.", fg=typer.colors.RED, err=True)
        return 1
    best, results = best_plan(mtf, cfg)
    report = render_report(mtf, results, best, sources)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    base = out_dir / f"{safe_symbol(symbol)}_{stamp}"
    md_path = base.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report, encoding="utf-8")
    setup_tf = mtf.setup_timeframe()
    assert setup_tf is not None  # noqa: S101 - checked above
    shown = best or max(results, key=lambda r: r.score.total, default=None)
    fig = build_figure(mtf.frames[setup_tf], shown, f"{symbol} {setup_tf}", cfg.chart_bars)
    html_path = write_chart(fig, base.with_suffix(".html"))
    typer.echo(report)
    typer.echo(f"Rapor: {md_path}\nGrafik: {html_path}")
    return 0


@app.command()
def analyze(
    symbol: Annotated[str, typer.Argument(help="ör. BTC/USDT")],
    exchange: Annotated[str, typer.Option("--exchange", "-e")] = "binance",
    data: Annotated[Path, typer.Option("--data", help="Veri kök klasörü")] = Path("data"),
    out: Annotated[Path, typer.Option("--out", help="Rapor klasörü")] = Path("reports/analysis"),
    offline: Annotated[bool, typer.Option("--offline", help="Yalnızca yerel Parquet")] = False,
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Çoklu zaman dilimi analizi: Türkçe rapor + işaretli plotly HTML grafik."""
    settings = _load_or_exit(config, env_file)
    try:
        code = asyncio.run(_analyze(settings, symbol, exchange, data, out, offline))
    except ExchangeAdapterError as exc:
        typer.secho(f"Veri alınamadı ({exc.kind}): {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=code)


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
