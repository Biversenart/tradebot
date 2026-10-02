"""Exchange announcement tracking (spec §5.13): delistings, maintenance, wallet suspensions.

Sources:
- `BinanceAnnouncementSource`: Binance's public CMS article list (through the egress session,
  CLAUDE.md rule 8). Titles are parsed with conservative regexes.
- `FileAnnouncementSource`: a user-edited YAML (`operations.announcements_file`) for anything
  the parser cannot see (maintenance windows, other exchanges).

Effects:
- delisting of an asset on an exchange -> no new trades in pairs containing it; open positions
  are closed when `operations.on_delisting: close`.
- maintenance with a time window -> no new trades on that exchange from
  `maintenance_block_before_minutes` before the start until the end.
- wallet suspension -> alert only (transfers are manual anyway).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import yaml

from bot.core.clock import Clock, SystemClock
from bot.core.events import AlertLevel
from bot.log import get_logger
from bot.ops.alerts import AlertSink, emit

_log = get_logger(__name__)
HttpGet = Callable[[str], Awaitable[str]]

BINANCE_CMS_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    "?type=1&catalogId={catalog}&pageNo=1&pageSize={size}"
)
# Binance CMS catalogs: 161 = Delisting, 157 = Maintenance Updates (assumption, see KARARLAR)
BINANCE_CATALOGS = (161, 157)


class AnnouncementKind(StrEnum):
    DELISTING = "delisting"
    MAINTENANCE = "maintenance"
    WALLET_SUSPENSION = "wallet_suspension"


@dataclass(frozen=True)
class Announcement:
    id: str
    exchange: str
    kind: AnnouncementKind
    title: str
    assets: tuple[str, ...] = ()
    published_at: datetime | None = None
    effective_at: datetime | None = None
    until: datetime | None = None


_TOKEN = re.compile(r"^[A-Z0-9]{2,15}$")
_DELIST = re.compile(r"\bdelist\s+(?P<assets>.+?)(?:\s+on\s+(?P<date>\d{4}-\d{2}-\d{2})|$)", re.I)
_WALLET = re.compile(r"suspend(?:s|ed)?\s+(?:deposits?\s+and\s+withdrawals?|withdrawals?)", re.I)
_MAINT = re.compile(r"(system\s+upgrade|scheduled\s+(?:system\s+)?maintenance)", re.I)
_PAREN = re.compile(r"\(([A-Z0-9]{2,15})\)")
_NOISE = {"BINANCE", "SPOT", "MARGIN", "FUTURES", "USDⓈ", "PERPETUAL", "CONTRACTS", "TRADING"}


def _assets(text: str) -> tuple[str, ...]:
    parts = re.split(r",|\band\b|&|/", text)
    out: list[str] = []
    for p in parts:
        tok = p.strip().upper()
        if _TOKEN.match(tok) and tok not in _NOISE and tok not in out:
            out.append(tok)
    return tuple(out)


def parse_binance_title(title: str) -> tuple[AnnouncementKind, tuple[str, ...], datetime | None]:
    """Classify a Binance announcement title. Raises ValueError if irrelevant."""
    m = _DELIST.search(title)
    if m:
        d = m.group("date")
        eff = datetime.fromisoformat(d).replace(tzinfo=UTC) if d else None
        return AnnouncementKind.DELISTING, _assets(m.group("assets")), eff
    if _WALLET.search(title):
        return AnnouncementKind.WALLET_SUSPENSION, tuple(dict.fromkeys(_PAREN.findall(title))), None
    if _MAINT.search(title):
        return AnnouncementKind.MAINTENANCE, (), None
    raise ValueError("ilgisiz duyuru")


class AnnouncementSource(Protocol):
    name: str

    async def fetch(self) -> list[Announcement]: ...


class BinanceAnnouncementSource:
    name = "binance_cms"

    def __init__(
        self, http_get: HttpGet, catalogs: Sequence[int] = BINANCE_CATALOGS, page_size: int = 20
    ) -> None:
        self.http_get = http_get
        self.catalogs = catalogs
        self.page_size = page_size

    @staticmethod
    def parse(body: str, exchange: str = "binance") -> list[Announcement]:
        data = json.loads(body)
        out: list[Announcement] = []
        for cat in (data.get("data") or {}).get("catalogs") or []:
            for art in cat.get("articles") or []:
                title = str(art.get("title", ""))
                try:
                    kind, assets, eff = parse_binance_title(title)
                except ValueError:
                    continue
                rel = art.get("releaseDate")
                out.append(
                    Announcement(
                        id=f"{exchange}:{art.get('id') or art.get('code') or title}",
                        exchange=exchange,
                        kind=kind,
                        title=title,
                        assets=assets,
                        published_at=datetime.fromtimestamp(int(rel) / 1000, UTC) if rel else None,
                        effective_at=eff,
                    )
                )
        return out

    async def fetch(self) -> list[Announcement]:
        out: list[Announcement] = []
        for cat in self.catalogs:
            body = await self.http_get(BINANCE_CMS_URL.format(catalog=cat, size=self.page_size))
            out += self.parse(body)
        return out


def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


class FileAnnouncementSource:
    """YAML list: [{exchange, kind, title, assets, effective_at, until}]"""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    async def fetch(self) -> list[Announcement]:
        if not self.path.exists():
            return []
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or []
        out = []
        for i, e in enumerate(raw):
            out.append(
                Announcement(
                    id=f"file:{e.get('id', i)}:{e['exchange']}:{e['kind']}",
                    exchange=str(e["exchange"]),
                    kind=AnnouncementKind(e["kind"]),
                    title=str(e.get("title", e["kind"])),
                    assets=tuple(str(a).upper() for a in e.get("assets", [])),
                    effective_at=_dt(e.get("effective_at")),
                    until=_dt(e.get("until")),
                )
            )
        return out


@dataclass
class AnnouncementWatcher:
    sources: list[AnnouncementSource]
    alert_sink: AlertSink | None = None
    clock: Clock = field(default_factory=SystemClock)
    maintenance_before: timedelta = timedelta(minutes=60)
    known: dict[str, Announcement] = field(default_factory=dict)
    _primed: bool = False

    async def refresh(self) -> list[Announcement]:
        """Fetch all sources; return (and alert on) announcements not seen before."""
        fresh: list[Announcement] = []
        for src in self.sources:
            try:
                items = await src.fetch()
            except Exception as exc:
                _log.warning("announcements_fetch_failed", source=src.name, error=str(exc))
                continue
            for a in items:
                if a.id not in self.known:
                    self.known[a.id] = a
                    fresh.append(a)
        for a in fresh:
            level = (
                AlertLevel.CRITICAL if a.kind is AnnouncementKind.DELISTING else AlertLevel.WARNING
            )
            await emit(
                self.alert_sink,
                level,
                f"exchange_{a.kind.value}",
                f"{a.exchange} duyurusu: {a.title}",
                exchange=a.exchange,
                assets=",".join(a.assets),
            )
        self._primed = True
        return fresh

    def _relevant(self, exchange: str) -> list[Announcement]:
        return [a for a in self.known.values() if a.exchange == exchange]

    def delisted_assets(self, exchange: str) -> set[str]:
        return {
            asset
            for a in self._relevant(exchange)
            if a.kind is AnnouncementKind.DELISTING
            for asset in a.assets
        }

    def block_reason(self, exchange: str, symbol: str) -> str | None:
        now = self.clock.now()
        assets = set(re.split(r"[/:]", symbol))
        hit = assets & self.delisted_assets(exchange)
        if hit:
            return f"delisting duyurusu: {', '.join(sorted(hit))} ({exchange})"
        for a in self._relevant(exchange):
            if a.kind is not AnnouncementKind.MAINTENANCE or a.effective_at is None:
                continue
            end = a.until or a.effective_at + timedelta(hours=2)
            if a.effective_at - self.maintenance_before <= now <= end:
                return f"bakım penceresi: {a.title} ({exchange})"
        return None
