from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import SecretStr

from bot.log import MASK, build_processors, configure_logging, mask_secrets, mask_text


def run_mask(**event: Any) -> dict[str, Any]:
    return dict(mask_secrets(None, "info", event))


def test_sensitive_keys_masked() -> None:
    out = run_mask(event="x", api_key="AKIA123", binance_api_secret="s3cr3t", token="t")
    assert out["api_key"] == MASK
    assert out["binance_api_secret"] == MASK
    assert out["token"] == MASK
    assert out["event"] == "x"


def test_secretstr_masked_under_any_key() -> None:
    assert run_mask(event="x", value=SecretStr("abc"))["value"] == MASK


def test_url_credentials_masked() -> None:
    out = run_mask(event="connecting to socks5://user:hunter2@1.2.3.4:1080 now")
    assert "hunter2" not in out["event"]
    assert "socks5://***@1.2.3.4:1080" in out["event"]


def test_nested_structures_masked() -> None:
    out = run_mask(
        event="x",
        details={"headers": {"X-MBX-APIKEY": "k", "Authorization": "B"}},
        items=[{"secret": "s"}],
    )
    assert out["details"]["headers"]["X-MBX-APIKEY"] == MASK
    assert out["details"]["headers"]["Authorization"] == MASK
    assert out["items"][0]["secret"] == MASK


def test_non_sensitive_values_untouched() -> None:
    out = run_mask(event="x", symbol="BTC/USDT", author="me", amount=1)
    assert out == {"event": "x", "symbol": "BTC/USDT", "author": "me", "amount": 1}


def test_mask_text_plain_url_without_credentials() -> None:
    assert mask_text("https://api.binance.com/api/v3") == "https://api.binance.com/api/v3"


def test_exception_text_is_masked() -> None:
    processors = build_processors()
    try:
        raise RuntimeError("proxy down: socks5://bob:topsecret@9.9.9.9:1080")
    except RuntimeError:
        event: Any = {"event": "fail", "exc_info": True}
        for proc in processors:
            event = proc(None, "error", event)
    assert isinstance(event, str)
    assert "topsecret" not in event
    data = json.loads(event)
    assert data["level"] == "error"
    assert "timestamp" in data


def test_configure_logging_emits_json(capsys: Any) -> None:
    configure_logging("INFO")
    structlog.get_logger("t").info("hello", api_secret="xyz", symbol="ETH/USDT")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(line)
    assert data["event"] == "hello"
    assert data["api_secret"] == MASK
    assert data["symbol"] == "ETH/USDT"
    structlog.reset_defaults()
