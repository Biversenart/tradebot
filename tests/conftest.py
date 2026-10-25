from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from bot.config.secrets import Secrets

_ENV_KEYS = [name.upper() for name in Secrets.model_fields]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests never read the developer's real environment secrets."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
