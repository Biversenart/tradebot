"""Real-browser check of the panel (demo data): every page renders without JS/CSP errors and
the kill switch flow works. Skipped when Playwright / Chromium is not installed."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import uvicorn
from pydantic import SecretStr

from bot.api.app import create_app
from bot.api.demo import build_demo

pw = pytest.importorskip("playwright.async_api")
TOKEN = "browser-test-token-123456"
PAGES = [
    "",
    "pozisyonlar",
    "grafik",
    "stratejiler",
    "risk",
    "arbitraj",
    "backtest",
    "analiz",
    "olaylar",
    "ayarlar",
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def server(tmp_path: Path) -> AsyncIterator[str]:
    control, panel, _ = await build_demo(SecretStr(TOKEN), tmp_path)
    port = free_port()
    srv = uvicorn.Server(
        uvicorn.Config(
            create_app(control, SecretStr(TOKEN), panel),
            port=port,
            log_level="error",
            lifespan="off",
        )
    )
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    await task
    await control.repo.close()


async def test_all_pages_render_and_kill_switch(server: str) -> None:
    errors: list[str] = []
    async with pw.async_playwright() as p:
        exe = os.environ.get("PANEL_TEST_CHROMIUM")
        try:
            browser = await p.chromium.launch(executable_path=exe, args=["--no-proxy-server"])
        except Exception as exc:  # browser binary missing / version mismatch
            pytest.skip(f"Chromium başlatılamadı (PANEL_TEST_CHROMIUM ile yol verin): {exc}")
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        await page.goto(server + "/")
        await page.fill("input[name=token]", TOKEN)
        await page.click("button[type=submit]")
        await page.wait_for_selector("#status-pills .pill")
        for slug in PAGES:
            await page.goto(f"{server}/#/{slug}")
            # (wait_for_function would need 'unsafe-eval', which the CSP rightly refuses)
            await page.wait_for_timeout(300)
            await page.wait_for_selector("#view .spinner", state="detached", timeout=10_000)
            assert "Hata" not in await page.inner_text("#view"), slug
        assert await page.is_hidden("#resume-btn")
        await page.click("#kill-btn")
        await page.click("#confirm-ok")
        await page.wait_for_selector("#banner:not([hidden])")
        assert "Kill switch aktif" in await page.inner_text("#banner")
        await page.click("#resume-btn")
        await page.click("#confirm-ok")
        await page.wait_for_selector("#banner", state="hidden")
        await browser.close()
    assert errors == []
