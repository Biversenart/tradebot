"""CLI entry point (`bot ...`)."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer

from bot import __version__
from bot.analysis.service import AnalysisUnavailableError, run_analysis
from bot.backtest.engine import Backtester
from bot.backtest.metrics import compute_metrics
from bot.backtest.runner import NoDataError, default_out_dir, load_history, run_report
from bot.backtest.sizing_compare import compare_sizing, sizing_markdown
from bot.config import ConfigError, Settings, load_settings
from bot.config.schema import ExchangeConfig
from bot.core.events import AlertLevel, RiskAlert
from bot.core.timeframes import timeframe_seconds
from bot.exchanges.errors import ExchangeAdapterError
from bot.exchanges.factory import build_public_adapter
from bot.log import configure_logging
from bot.marketdata.history import download_to_parquet, parquet_path, utc_date
from bot.marketdata.synthetic import generate_ohlcv
from bot.net.egress import resolve_egress
from bot.ops import commands as ops_commands
from bot.ops.capital import CapitalCapError
from bot.runtime import BotRuntime
from bot.strategies.registry import STRATEGIES, build_strategy

app = typer.Typer(help="Kripto trade & arbitraj botu", no_args_is_help=True)
data_app = typer.Typer(help="Geçmiş veri komutları", no_args_is_help=True)
app.add_typer(data_app, name="data")
bt_app = typer.Typer(help="Backtest komutları", no_args_is_help=True)
app.add_typer(bt_app, name="backtest")
capital_app = typer.Typer(help="Kanarya sermaye tavanı (live)", no_args_is_help=True)
app.add_typer(capital_app, name="capital")
shadow_app = typer.Typer(help="Gölge mod (parametre değişiklikleri)", no_args_is_help=True)
app.add_typer(shadow_app, name="shadow")

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
    try:
        out = await run_analysis(settings, symbol, exchange, data_root, out_dir, offline)
    except AnalysisUnavailableError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        return 1
    typer.echo(out.report)
    typer.echo(f"Rapor: {out.md_path}\nGrafik: {out.html_path}")
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


def _bt_frames(
    symbols: list[str], tf: str, exchange: str, data: Path, since: str | None, until: str | None
) -> dict[str, pd.DataFrame]:
    try:
        return {
            s: load_history(
                data,
                exchange,
                s,
                tf,
                utc_date(since) if since else None,
                utc_date(until) if until else None,
            )
            for s in symbols
        }
    except NoDataError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


SymbolsOpt = Annotated[list[str], typer.Option("--symbol", "-s", help="tekrarlanabilir")]
StrategyOpt = Annotated[
    list[str], typer.Option("--strategy", help="tekrarlanabilir; boş: etkin olanlar")
]


@bt_app.command("run")
def backtest_run(
    symbol: SymbolsOpt,
    strategy: Annotated[str, typer.Option("--strategy")],
    tf: Annotated[str, typer.Option("--tf")] = "1h",
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    exchange: Annotated[str, typer.Option("--exchange", "-e")] = "binance",
    data: Annotated[Path, typer.Option("--data")] = Path("data"),
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Tek parametre setiyle backtest (örneklem içi; karar için walkforward kullanın)."""
    settings = _load_or_exit(config, env_file)
    frames = _bt_frames(symbol, tf, exchange, data, since, until)
    cfg = settings.config
    res = Backtester(frames, lambda s: build_strategy(cfg, strategy, s, exchange), cfg).run()
    m = compute_metrics(res)
    for k, v in m.as_dict().items():
        typer.echo(f"{k:>18}: {v}")
    typer.echo("Not: Bu sonuç örneklem içidir; §9 kararı için 'bot backtest walkforward' kullanın.")


@bt_app.command("walkforward")
def backtest_walkforward(
    symbol: Annotated[str, typer.Option("--symbol", "-s")],
    strategy: Annotated[str, typer.Option("--strategy")],
    tf: Annotated[str, typer.Option("--tf")] = "1h",
    since: Annotated[str | None, typer.Option("--since")] = None,
    until: Annotated[str | None, typer.Option("--until")] = None,
    exchange: Annotated[str, typer.Option("--exchange", "-e")] = "binance",
    data: Annotated[Path, typer.Option("--data")] = Path("data"),
    out: Annotated[Path | None, typer.Option("--out")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Walk-forward + parametre taraması; HTML rapor (örneklem dışı sonuçlar)."""
    settings = _load_or_exit(config, env_file)
    frames = _bt_frames([symbol], tf, exchange, data, since, until)
    summary, htmls, _results = run_report(
        settings.config,
        frames,
        [strategy],
        out or default_out_dir(),
        "Yerel Parquet verisi.",
        timeframe=tf,
    )
    typer.echo(summary.read_text(encoding="utf-8"))
    typer.echo(f"HTML: {htmls[0]}")


@bt_app.command("report")
def backtest_report(
    symbol: SymbolsOpt,
    strategy: StrategyOpt = [],  # noqa: B006
    tf: Annotated[str, typer.Option("--tf")] = "1h",
    since: Annotated[str | None, typer.Option("--since")] = "2022-01-01",
    until: Annotated[str | None, typer.Option("--until")] = None,
    exchange: Annotated[str, typer.Option("--exchange", "-e")] = "binance",
    data: Annotated[Path, typer.Option("--data")] = Path("data"),
    out: Annotated[Path | None, typer.Option("--out")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Çoklu sembol x strateji walk-forward raporu + §9 karşılaştırmalı OZET.md."""
    settings = _load_or_exit(config, env_file)
    names = strategy or [
        n for n, c in settings.config.strategies.items() if c.enabled and n in STRATEGIES
    ]
    if not names:
        names = list(STRATEGIES)
    frames = _bt_frames(symbol, tf, exchange, data, since, until)
    span = ", ".join(
        f"{s}: {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}" for s, df in frames.items()
    )
    summary, _htmls, _ = run_report(
        settings.config,
        frames,
        names,
        out or default_out_dir(),
        f"Veri: {exchange} {tf} ({span}).",
        timeframe=tf,
    )
    typer.echo(summary.read_text(encoding="utf-8"))
    typer.echo(f"Özet: {summary}\nHTML raporlar: {summary.parent}")


@data_app.command("synthetic")
def data_synthetic(
    symbol: Annotated[list[str], typer.Option("--symbol", "-s")],
    since: Annotated[str, typer.Option("--since")] = "2022-01-01",
    until: Annotated[str | None, typer.Option("--until")] = None,
    tf: Annotated[str, typer.Option("--tf")] = "1h",
    price: Annotated[float, typer.Option("--price", help="başlangıç fiyatı")] = 100.0,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    out: Annotated[Path, typer.Option("--out")] = Path("data"),
) -> None:
    """SENTETİK veri üret (borsa adı 'synthetic'; gerçek veriyle karışmaz). Demo/test için."""
    end = utc_date(until) if until else datetime.now(UTC)
    for k, sym in enumerate(symbol):
        df = generate_ohlcv(utc_date(since), end, tf, price, seed + k)
        path = parquet_path(out, "synthetic", sym, tf)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
        typer.echo(f"{sym}: {len(df)} sentetik mum -> {path}")


@bt_app.command("sizing")
def backtest_sizing(
    symbol: SymbolsOpt,
    strategy: StrategyOpt = [],  # noqa: B006
    tf: Annotated[str, typer.Option("--tf")] = "1h",
    since: Annotated[str | None, typer.Option("--since")] = "2022-01-01",
    until: Annotated[str | None, typer.Option("--until")] = None,
    exchange: Annotated[str, typer.Option("--exchange", "-e")] = "binance",
    data: Annotated[Path, typer.Option("--data")] = Path("data"),
    out: Annotated[Path | None, typer.Option("--out")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Sabit %1 risk ile büyüme odaklı boyutlamayı aynı sinyallerde karşılaştır."""
    settings = _load_or_exit(config, env_file)
    names = strategy or [
        n for n, c in settings.config.strategies.items() if c.enabled and n in STRATEGIES
    ]
    frames = _bt_frames(symbol, tf, exchange, data, since, until)
    rows = compare_sizing(frames, names or list(STRATEGIES), settings.config)
    md = sizing_markdown(rows, f"Veri: {exchange} {tf}")
    path = (out or default_out_dir()) / "BOYUTLAMA.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    typer.echo(md)
    typer.echo(f"Rapor: {path}")


OperatorOpt = Annotated[str, typer.Option("--by", help="Onaylayan kişi")]
_OFFLINE_NOTE = (
    "Not: bot çalışıyorsa panel/Telegram kullanın (çalışan bot bu durumu üzerine yazar)."
)


@capital_app.command("show")
def capital_show(config: ConfigOption = DEFAULT_CONFIG, env_file: EnvOption = DEFAULT_ENV) -> None:
    """Geçerli live sermaye tavanı ve geçmişi."""
    settings = _load_or_exit(config, env_file)
    typer.echo(asyncio.run(ops_commands.capital_show(settings)))


@capital_app.command("raise")
def capital_raise(
    pct: Annotated[str, typer.Argument(help="Yeni tavan yüzdesi (ör. 20)")],
    by: OperatorOpt = "cli",
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Tavanı manuel onayla değiştir (kanarya süresi ve kademeli artış kuralı uygulanır)."""
    settings = _load_or_exit(config, env_file)
    try:
        typer.echo(asyncio.run(ops_commands.capital_raise(settings, Decimal(pct), by)))
    except (CapitalCapError, InvalidOperation) as exc:
        typer.secho(f"Reddedildi: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(_OFFLINE_NOTE)


@shadow_app.command("report")
def shadow_report(config: ConfigOption = DEFAULT_CONFIG, env_file: EnvOption = DEFAULT_ENV) -> None:
    """Strateji parametrelerinin onay durumu ve gölge/canlı karşılaştırması."""
    settings = _load_or_exit(config, env_file)
    typer.echo(asyncio.run(ops_commands.shadow_report(settings)))


@shadow_app.command("approve")
def shadow_approve(
    name: Annotated[str, typer.Argument(help="Strateji adı (config anahtarı)")],
    by: OperatorOpt = "cli",
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Gölgedeki parametreleri onayla; bir sonraki başlatmada canlıda çalışır."""
    settings = _load_or_exit(config, env_file)
    try:
        typer.echo(asyncio.run(ops_commands.shadow_approve(settings, name, by)))
    except KeyError as exc:
        typer.secho(f"Strateji bulunamadı: {name}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(_OFFLINE_NOTE)


@app.command("config-log")
def config_log(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    config: ConfigOption = DEFAULT_CONFIG,
    env_file: EnvOption = DEFAULT_ENV,
) -> None:
    """Config değişiklik günlüğü (dosya, panel, Telegram, CLI)."""
    settings = _load_or_exit(config, env_file)
    typer.echo(asyncio.run(ops_commands.config_log(settings, limit)))


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
