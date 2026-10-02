from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.core.clock import ManualClock
from bot.core.events import AlertLevel, RiskAlert
from bot.ops.announcements import (
    Announcement,
    AnnouncementKind,
    AnnouncementWatcher,
    BinanceAnnouncementSource,
    FileAnnouncementSource,
    parse_binance_title,
)

FIX = Path(__file__).parents[2] / "fixtures" / "announcements" / "binance_cms.json"
T = datetime(2026, 3, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("title", "kind", "assets"),
    [
        (
            "Binance Will Delist ANT, MULTI, VAI and XMR on 2024-02-20",
            "delisting",
            ("ANT", "MULTI", "VAI", "XMR"),
        ),
        ("Binance Will Delist WAVES", "delisting", ("WAVES",)),
        (
            "Binance Will Suspend Deposits and Withdrawals on the Solana Network (SOL)",
            "wallet_suspension",
            ("SOL",),
        ),
        ("Binance Will Perform Scheduled System Maintenance", "maintenance", ()),
    ],
)
def test_parse_titles(title: str, kind: str, assets: tuple[str, ...]) -> None:
    k, a, _ = parse_binance_title(title)
    assert k.value == kind and a == assets


def test_parse_irrelevant_and_effective_date() -> None:
    with pytest.raises(ValueError):
        parse_binance_title("Binance Will List TOKEN (TKN)")
    _, _, eff = parse_binance_title("Binance Will Delist X1 on 2024-02-20")
    assert eff == datetime(2024, 2, 20, tzinfo=UTC)


def test_parse_cms_payload() -> None:
    items = BinanceAnnouncementSource.parse(FIX.read_text())
    kinds = [a.kind for a in items]
    assert kinds == [
        AnnouncementKind.DELISTING,
        AnnouncementKind.WALLET_SUSPENSION,
        AnnouncementKind.MAINTENANCE,
    ]  # "Notice of Removal" has no pairs in the title -> skipped
    assert items[0].id == "binance:201" and items[0].published_at is not None


async def test_source_uses_injected_http_get() -> None:
    urls: list[str] = []

    async def get(url: str) -> str:
        urls.append(url)
        return FIX.read_text()

    src = BinanceAnnouncementSource(get, catalogs=(161,))
    assert len(await src.fetch()) == 3
    assert "catalogId=161" in urls[0] and urls[0].startswith("https://www.binance.com/")


class Static:
    name = "static"

    def __init__(self, items: list[Announcement]) -> None:
        self.items = items
        self.fail = False

    async def fetch(self) -> list[Announcement]:
        if self.fail:
            raise RuntimeError("down")
        return self.items


async def test_watcher_blocks_delisted_pairs_and_alerts_once() -> None:
    alerts: list[RiskAlert] = []

    async def sink(a: RiskAlert) -> None:
        alerts.append(a)

    src = Static(
        [
            Announcement("1", "binance", AnnouncementKind.DELISTING, "delist XMR", ("XMR",)),
            Announcement(
                "2", "binance", AnnouncementKind.WALLET_SUSPENSION, "SOL wallet", ("SOL",)
            ),
        ]
    )
    w = AnnouncementWatcher([src], sink, ManualClock(T))
    assert len(await w.refresh()) == 2
    assert await w.refresh() == []  # dedup
    assert [a.level for a in alerts] == [AlertLevel.CRITICAL, AlertLevel.WARNING]
    assert "XMR" in (w.block_reason("binance", "XMR/USDT") or "")
    assert w.block_reason("binance", "SOL/USDT") is None  # wallet suspension: alert only
    assert w.block_reason("btcturk", "XMR/USDT") is None  # other exchange
    src.fail = True
    assert await w.refresh() == []  # broken source does not raise


async def test_maintenance_window_from_file(tmp_path: Path) -> None:
    f = tmp_path / "ann.yaml"
    f.write_text(
        "- exchange: binance\n  kind: maintenance\n  title: Sistem bakımı\n"
        "  effective_at: 2026-03-01T14:00:00Z\n  until: 2026-03-01T16:00:00Z\n"
        "- exchange: binance\n  kind: delisting\n  assets: [ftt]\n"
    )
    clock = ManualClock(T)
    w = AnnouncementWatcher(
        [FileAnnouncementSource(f)], clock=clock, maintenance_before=timedelta(minutes=60)
    )
    await w.refresh()
    assert w.block_reason("binance", "BTC/USDT") is None  # 12:00, window opens 13:00
    clock.advance(timedelta(hours=1, minutes=30))
    assert "bakım" in (w.block_reason("binance", "BTC/USDT") or "")
    clock.advance(timedelta(hours=3))
    assert w.block_reason("binance", "BTC/USDT") is None
    assert w.block_reason("binance", "FTT/USDT") is not None
    assert await FileAnnouncementSource(tmp_path / "missing.yaml").fetch() == []
