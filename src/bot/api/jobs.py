"""Background jobs started from the panel: backtest reports and historical data downloads.

One job runs at a time (CPU / exchange rate limits); history keeps the last 30. Backtests run
in a worker thread on local Parquet data. Downloads use the injected `download` coroutine,
which goes through the egress like `bot data download` (CLAUDE.md rule 8). Jobs never place
orders.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from bot.backtest.runner import NoDataError, load_history, run_report
from bot.config.schema import AppConfig
from bot.core.timeframes import timeframe_seconds
from bot.log import get_logger
from bot.marketdata.history import utc_date
from bot.strategies.registry import STRATEGIES

_log = get_logger(__name__)
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,15}/[A-Z0-9]{2,15}$")
DownloadFn = Callable[[str, list[str], list[str], datetime, datetime | None], Awaitable[list[str]]]


class JobError(ValueError):
    pass


@dataclass
class Job:
    id: str
    kind: str  # backtest | download
    params: dict[str, Any]
    status: str = "queued"  # queued | running | done | failed
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "message": self.message,
            "result": self.result,
        }


def _symbols(raw: list[str]) -> list[str]:
    out = []
    for s in raw:
        s = s.strip().upper()
        if "/" not in s:
            s += "/USDT"
        if not SYMBOL_RE.match(s):
            raise JobError(f"Geçersiz sembol: {s}")
        out.append(s)
    if not out or len(out) > 10:
        raise JobError("1–10 sembol girin.")
    return out


def _tf(raw: str) -> str:
    try:
        timeframe_seconds(raw)
    except Exception as exc:
        raise JobError(f"Geçersiz zaman dilimi: {raw}") from exc
    return raw


def _date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return utc_date(raw)
    except ValueError as exc:
        raise JobError(f"Geçersiz tarih: {raw}") from exc


class JobRunner:
    def __init__(
        self,
        config: AppConfig,
        data_root: Path = Path("data"),
        reports_root: Path = Path("reports/backtest"),
        download: DownloadFn | None = None,
        exchange: str = "binance",
    ) -> None:
        self.config = config
        self.data_root = data_root
        self.reports_root = reports_root
        self.download_fn = download
        self.exchange = exchange
        self.jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    # ---------------------------------------------------------------- queries
    def list_jobs(self) -> list[dict[str, Any]]:
        return [
            j.to_dict()
            for j in sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        ]

    def datasets(self) -> list[dict[str, Any]]:
        root = self.data_root / "ohlcv"
        out: list[dict[str, Any]] = []
        if not root.exists():
            return out
        for f in sorted(root.glob("*/*/*.parquet")):
            exchange, sym, tf = f.parent.parent.name, f.parent.name, f.stem
            item: dict[str, Any] = {
                "exchange": exchange,
                "symbol": sym.replace("-", "/"),
                "timeframe": tf,
                "size_kb": round(f.stat().st_size / 1024),
            }
            try:
                idx = pd.read_parquet(f, columns=["close"]).index
                if len(idx):
                    item |= {"bars": len(idx), "start": str(idx.min()), "end": str(idx.max())}
            except Exception as exc:  # a broken file is listed without details
                _log.debug("dataset_read_failed", path=str(f), error=str(exc))
            out.append(item)
        return out

    def runs(self) -> list[dict[str, Any]]:
        if not self.reports_root.exists():
            return []
        out = []
        for d in sorted(self.reports_root.iterdir(), reverse=True):
            if d.is_dir() and (d / "OZET.md").exists():
                out.append(
                    {"run": d.name, "files": sorted(p.name for p in d.iterdir() if p.is_file())}
                )
        return out[:50]

    def report_file(self, run: str, name: str) -> Path | None:
        if any(x in run + name for x in ("/", "\\")) or run.startswith(".") or name.startswith("."):
            return None
        path = (self.reports_root / run / name).resolve()
        if path.parent.parent != self.reports_root.resolve() or not path.is_file():
            return None
        return path if path.suffix in (".md", ".html") else None

    # ---------------------------------------------------------------- submit
    def _start(self, job: Job, work: Callable[[Job], Awaitable[None]]) -> Job:
        if any(j.status in ("queued", "running") for j in self.jobs.values()):
            raise JobError("Başka bir iş çalışıyor; bitmesini bekleyin.")
        self.jobs[job.id] = job
        for old in sorted(self.jobs.values(), key=lambda j: j.created_at)[:-30]:
            self.jobs.pop(old.id, None)

        async def run() -> None:
            async with self._lock:
                job.status = "running"
                try:
                    await work(job)
                    job.status = "done"
                except (JobError, NoDataError) as exc:
                    job.status, job.message = "failed", str(exc)
                except Exception as exc:
                    _log.exception("panel_job_failed", job=job.id)
                    job.status, job.message = "failed", f"{type(exc).__name__}: {exc}"
                finally:
                    job.finished_at = datetime.now(UTC)

        task = asyncio.create_task(run(), name=f"job:{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def submit_backtest(
        self,
        symbols: list[str],
        strategies: list[str],
        timeframe: str = "1h",
        since: str | None = None,
        until: str | None = None,
    ) -> Job:
        syms, tf = _symbols(symbols), _tf(timeframe)
        names = (
            strategies
            or [n for n, c in self.config.strategies.items() if c.enabled and n in STRATEGIES]
            or list(STRATEGIES)
        )
        unknown = [n for n in names if n not in STRATEGIES]
        if unknown:
            raise JobError(f"Bilinmeyen strateji: {', '.join(unknown)}")
        start, end = _date(since), _date(until)
        job = Job(
            uuid.uuid4().hex[:10],
            "backtest",
            {"symbols": syms, "strategies": names, "timeframe": tf, "since": since, "until": until},
        )

        def compute() -> dict[str, Any]:
            frames = {
                s: load_history(self.data_root, self.exchange, s, tf, start, end) for s in syms
            }
            out = self.reports_root / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            span = ", ".join(
                f"{s}: {df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d}"
                for s, df in frames.items()
                if len(df)
            )
            summary, _, _ = run_report(
                self.config, frames, names, out, f"Veri: {self.exchange} {tf} ({span}).", tf
            )
            return {"run": out.name, "summary": summary.name}

        async def work(j: Job) -> None:
            j.result = await asyncio.to_thread(compute)
            j.message = "Rapor hazır."

        return self._start(job, work)

    def submit_download(
        self, symbols: list[str], timeframes: list[str], since: str, until: str | None = None
    ) -> Job:
        if self.download_fn is None:
            raise JobError("Veri indirme bu çalıştırmada kullanılamıyor.")
        syms = _symbols(symbols)
        tfs = [_tf(t) for t in timeframes] or ["1h"]
        start = _date(since)
        if start is None:
            raise JobError("Başlangıç tarihi gerekli.")
        end = _date(until)
        job = Job(
            uuid.uuid4().hex[:10],
            "download",
            {"symbols": syms, "timeframes": tfs, "since": since, "until": until},
        )
        fn = self.download_fn

        async def work(j: Job) -> None:
            lines = await fn(self.exchange, syms, tfs, start, end)
            j.result = {"lines": lines}
            j.message = "İndirme tamamlandı."

        return self._start(job, work)
