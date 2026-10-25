"""Static rules from CLAUDE.md enforced on the source tree."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"


def py_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_only_exchanges_package_imports_ccxt() -> None:
    pattern = re.compile(r"^\s*(import ccxt|from ccxt)", re.M)
    offenders = [
        str(p.relative_to(SRC))
        for p in py_files()
        if pattern.search(p.read_text("utf-8")) and p.parent.name != "exchanges"
    ]
    assert offenders == []


def test_no_withdraw_code() -> None:
    """CLAUDE.md rule 3: the bot never withdraws funds."""
    pattern = re.compile(r"\bwithdraw\w*\s*\(", re.I)
    offenders = [
        str(p.relative_to(SRC)) for p in py_files() if pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == []


def test_strategies_never_touch_adapters() -> None:
    """Strategies produce OrderIntents only (rule 4)."""
    strat = SRC / "strategies"
    for p in strat.rglob("*.py"):
        text = p.read_text("utf-8")
        assert "bot.exchanges" not in text, p
        assert "create_order" not in text, p
