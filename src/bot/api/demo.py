"""`bot panel-demo`: the web panel on SYNTHETIC data, without exchanges or a running bot.

Everything lives in a temporary directory (SQLite DB, synthetic Parquet history under the
exchange name "synthetic", reports) and is deleted on exit. No network, no orders, no keys.
Used to preview the UI and for the panel's browser tests.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
from pydantic import SecretStr

from bot.analysis.service import AnalysisOutput, run_analysis
from bot.api.jobs import JobRunner
from bot.api.views import PanelData
from bot.config import Mode, Settings
from bot.config.schema import AppConfig
from bot.config.secrets import Secrets
from bot.control import BotControl
from bot.core.events import AlertLevel, RiskAlert
from bot.core.models import Order, OrderStatus, OrderType, Side
from bot.marketdata.history import parquet_path
from bot.marketdata.synthetic import generate_ohlcv
from bot.ops.config_log import ConfigChangeLog
from bot.ops.service import OpsService
from bot.ops.shadow import ShadowBook, ShadowRegistry
from bot.risk.kill_switch import KillSwitch
from bot.storage.models import PositionRow
from bot.storage.repository import Repository
from bot.strategies.registry import build_enabled

EXCHANGE = "synthetic"
SYMBOLS = {"BTC/USDT": 60_000.0, "ETH/USDT": 3_000.0, "SOL/USDT": 150.0}
STRATS = ("ema_crossover", "breakout", "rsi_reversion")
D = Decimal


def demo_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "exchanges": {EXCHANGE: {"enabled": True}},
            "universe": {"symbols": list(SYMBOLS)},
            "strategies": {
                "ema_crossover": {"enabled": True, "fast": 9, "slow": 21, "timeframe": "1h"},
                "breakout": {"enabled": True, "donchian": 20, "timeframe": "1h"},
                "rsi_reversion": {"enabled": True, "period": 14, "timeframe": "1h"},
            },
            "marketdata": {"candle_timeframes": ["15m", "1h", "4h", "1d"]},
            "risk": {"growth": {"profit_lock": {"enabled": False}}},
        }
    )


def _write_history(root: Path, now: datetime) -> dict[str, pd.DataFrame]:
    frames = {}
    for k, (sym, price) in enumerate(SYMBOLS.items()):
        df = generate_ohlcv(now - timedelta(days=400), now, "1h", price, seed=11 + k)
        path = parquet_path(root, EXCHANGE, sym, "1h")
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)
        fine = generate_ohlcv(
            now - timedelta(days=30), now, "15m", float(df["close"].iloc[-1]), 31 + k
        )
        fine.to_parquet(parquet_path(root, EXCHANGE, sym, "15m"))
        frames[sym] = df
    return frames


async def _seed(repo: Repository, frames: dict[str, pd.DataFrame], now: datetime) -> None:
    rng = random.Random(7)  # noqa: S311 - demo data, not security
    equity = D(10_000)
    start = now - timedelta(days=60)
    trades: list[tuple[datetime, PositionRow]] = []
    for i in range(48):
        sym = rng.choice(list(SYMBOLS))
        df = frames[sym]
        opened = start + timedelta(hours=rng.randint(0, 24 * 57))
        hold = timedelta(hours=rng.randint(3, 40))
        closed = min(opened + hold, now - timedelta(hours=2))
        close = df["close"]
        entry = D(str(round(float(close[close.index <= opened].iloc[-1]), 4)))
        exit_ = D(str(round(float(close[close.index <= closed].iloc[-1]), 4)))
        long = rng.random() < 0.7
        risk_amt = D(100)
        stop = entry * (D("0.97") if long else D("1.03"))
        qty = (risk_amt / abs(entry - stop)).quantize(D("0.0001"))
        pnl = ((exit_ - entry) if long else (entry - exit_)) * qty
        fees = (entry + exit_) * qty * D("0.001")
        pid = f"demo{i:03d}"
        row = PositionRow(
            position_id=pid,
            exchange=EXCHANGE,
            symbol=sym,
            side="long" if long else "short",
            strategy=rng.choice(STRATS),
            timeframe="1h",
            amount=D(0),
            initial_amount=qty,
            entry_price=entry,
            initial_stop=stop,
            stop_loss=stop,
            take_profits=[str(entry * (D("1.06") if long else D("0.94")))],
            realized_pnl=(pnl - fees).quantize(D("0.01")),
            fees=fees.quantize(D("0.0001")),
            status="closed",
            close_reason=rng.choice(["tp1", "stop", "trailing_stop", "time"]),
            opened_at=opened,
            closed_at=closed,
        )
        await repo.save_position(row)
        for role, t, px, side in (
            ("entry", opened, entry, Side.BUY if long else Side.SELL),
            ("exit", closed, exit_, Side.SELL if long else Side.BUY),
        ):
            await repo.save_order(
                Order(
                    client_order_id=f"tb{pid}{role}",
                    exchange=EXCHANGE,
                    symbol=sym,
                    side=side,
                    order_type=OrderType.MARKET,
                    amount=qty,
                    status=OrderStatus.FILLED,
                    filled=qty,
                    average_price=px,
                    created_at=t,
                    updated_at=t,
                ),
                role,
                pid,
                row.strategy,
            )
        trades.append((closed, row))
    trades.sort(key=lambda x: x[0])
    t, j = start, 0
    while t <= now:
        while j < len(trades) and trades[j][0] <= t:
            equity += trades[j][1].realized_pnl
            j += 1
        await repo.save_equity(equity + D(str(round(rng.uniform(-25, 25), 2))), D(0), t)
        t += timedelta(hours=4)
    for k, sym in enumerate(["BTC/USDT", "ETH/USDT"]):
        last = D(str(round(float(frames[sym]["close"].iloc[-30]), 4)))
        await repo.save_position(
            PositionRow(
                position_id=f"open{k}",
                exchange=EXCHANGE,
                symbol=sym,
                side="long",
                strategy=STRATS[k],
                timeframe="1h",
                amount=(D(60) / (last * D("0.03"))).quantize(D("0.0001")),
                initial_amount=(D(60) / (last * D("0.03"))).quantize(D("0.0001")),
                entry_price=last,
                initial_stop=last * D("0.97"),
                stop_loss=last * D("0.97"),
                stop_order_id=f"tbopen{k}stop",
                take_profits=[str(last * D("1.03")), str(last * D("1.06")), str(last * D("1.09"))],
                status="open",
                opened_at=now - timedelta(hours=30),
            )
        )
    for level, code, msg in (
        (AlertLevel.INFO, "reconcile_done", "Reconciliation: güncellenen emir 0, hata 0"),
        (AlertLevel.WARNING, "exchange_balance_cap", "synthetic bakiyesi 10500 > tavan 5000"),
        (AlertLevel.CRITICAL, "egress_ip_unreachable", "Dış IP doğrulaması başarısız (demo)."),
        (AlertLevel.INFO, "egress_ip_recovered", "Dış IP yeniden doğrulandı."),
    ):
        await repo.save_risk_event(RiskAlert(level=level, code=code, message=msg))
    for i in range(30):
        await repo.save_arbitrage(
            "cross" if i % 3 else "triangular",
            "synthetic→binance BTC/USDT" if i % 3 else "USDT→BTC→ETH→USDT",
            D(str(round(rng.uniform(0.25, 0.9), 3))),
            D(str(rng.randint(100, 1000))),
            i % 4 == 0,
            {},
        )


async def build_demo(
    token: SecretStr, root: Path | None = None
) -> tuple[BotControl, PanelData, Path]:
    base = root or Path(tempfile.mkdtemp(prefix="tradebot-demo-"))
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    data_root = base / "data"
    frames = _write_history(data_root, now)
    cfg = demo_config()
    db = f"sqlite+aiosqlite:///{base / 'demo.db'}"
    settings = Settings(cfg, Secrets(_env_file=None, database_url=SecretStr(db)), Mode.PAPER)
    repo = await Repository.connect(db)
    await _seed(repo, frames, now)
    log = ConfigChangeLog(repo)
    await log.sync_file_config(cfg)
    await log.record("runtime.kill_switch", False, True, "panel", "Panel (demo)")
    await log.record("runtime.kill_switch", True, False, "panel", "Panel (demo)")
    ks = KillSwitch(cfg.risk)
    points = await repo.equity_curve(5000)
    ks.state.peak_equity = max(p.equity for p in points)
    ks.state.day_start_equity = points[-7].equity if len(points) > 7 else points[-1].equity
    reports = base / "reports"
    strategies = [s for sym in SYMBOLS for s in build_enabled(cfg, sym, EXCHANGE)]

    async def analyze(symbol: str) -> AnalysisOutput:
        return await run_analysis(
            settings, symbol, EXCHANGE, data_root, reports / "analysis", offline=True
        )

    curve = await repo.equity_curve(1)
    control = BotControl(
        settings,
        repo,
        ks,
        strategies,
        equity=lambda: curve[-1].equity if curve else D(10_000),
        analyze=analyze,
        reports_dir=reports / "analysis",
        config_log=log,
        ops=OpsService(cfg, Mode.PAPER, {}, sources=[]),
        shadow=(ShadowRegistry(), ShadowBook()),
    )
    jobs = JobRunner(cfg, data_root, reports / "backtest", exchange=EXCHANGE)

    def mark(_exchange: str, symbol: str) -> Decimal | None:
        df = frames.get(symbol)
        return D(str(round(float(df["close"].iloc[-1]), 4))) if df is not None else None

    panel = PanelData(control, jobs=jobs, data_root=data_root, default_exchange=EXCHANGE, mark=mark)
    return control, panel, base


async def demo_ticker(panel: PanelData, stop: asyncio.Event, every: float = 45) -> None:
    """Push an occasional demo alert so live toasts can be seen."""
    n = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), every)
        except TimeoutError:
            n += 1
            await panel._on_alert(
                RiskAlert(level=AlertLevel.INFO, code="demo", message=f"Demo canlı olay #{n}")
            )
