from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from bot.config import (
    AppConfig,
    ConfigError,
    EgressMode,
    Mode,
    Secrets,
    load_app_config,
    load_settings,
    resolve_trading_mode,
)
from bot.config.schema import ExchangeConfig

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "config.example.yaml"
GLOBAL_IP = "8.8.8.8"


def secrets(**kwargs: Any) -> Secrets:
    return Secrets(_env_file=None, **kwargs)


def config(**kwargs: Any) -> AppConfig:
    return AppConfig.model_validate(kwargs)


def egress_ok() -> dict[str, Any]:
    return {
        "egress_proxy_url": SecretStr("socks5://u:p@1.2.3.4:1080"),
        "egress_expected_ip": GLOBAL_IP,
    }


# --------------------------------------------------------------------- loading


def test_example_config_is_valid() -> None:
    cfg = load_app_config(EXAMPLE)
    assert cfg.mode is Mode.PAPER
    assert cfg.live_trading_confirmed is False
    assert cfg.egress.fail_closed is True
    assert cfg.risk.risk_per_trade_pct == 1
    assert "binance" in cfg.enabled_exchanges


def test_missing_config_file_gives_paper_defaults(tmp_path: Path) -> None:
    cfg = load_app_config(tmp_path / "yok.yaml")
    assert cfg.mode is Mode.PAPER
    assert cfg.live_trading_confirmed is False


def test_unknown_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  risk_per_trad_pct: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(p)


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(p)


def test_invalid_yaml_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(p)


def test_decimals_parsed_exactly() -> None:
    cfg = load_app_config(EXAMPLE)
    assert str(cfg.universe.scanner.max_spread_pct) == "0.05"


@pytest.mark.parametrize(
    "override",
    [
        {"egress": {"fail_closed": False}},
        {"egress": {"ip_check_services": ["https://api.ipify.org"]}},
        {"egress": {"ip_check_services": ["https://a.example", "https://a.example"]}},
        {"egress": {"ip_check_services": ["http://a.example", "https://b.example"]}},
        {"risk": {"max_leverage": 5}},
        {"risk": {"growth": {"kelly": {"fraction": 0.5}}}},
        {"risk": {"growth": {"kelly": {"min_trades": 10}}}},
        {"risk": {"growth": {"quality_multiplier": {"high_score": 2}}}},
        {"risk": {"max_exposure_per_symbol_pct": 80, "max_total_exposure_pct": 60}},
        {"analysis": {"trade_plan": {"take_profits": [2, 1]}}},
        {"analysis": {"confluence": {"weights": {"a": -1}}}},
        {"universe": {"symbols": ["BTCUSDT"]}},
        {"api": {"port": 0}},
    ],
)
def test_unsafe_or_invalid_values_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(override)


# --------------------------------------------------------------------- mode resolution


def test_default_mode_is_paper() -> None:
    assert resolve_trading_mode(secrets(), config()) is Mode.PAPER


def test_env_and_config_mode_must_agree() -> None:
    with pytest.raises(ConfigError, match="uyuşmuyor"):
        resolve_trading_mode(secrets(trading_mode="testnet"), config(mode="paper"))


def test_live_requires_env_live() -> None:
    cfg = config(
        mode="live",
        live_trading_confirmed=True,
        exchanges={"binance": {"enabled": True, "testnet": False}},
    )
    with pytest.raises(ConfigError, match="TRADING_MODE=live"):
        resolve_trading_mode(secrets(**egress_ok()), cfg)


def test_live_requires_confirmation_flag() -> None:
    cfg = config(
        mode="live",
        live_trading_confirmed=False,
        exchanges={"binance": {"enabled": True, "testnet": False}},
    )
    with pytest.raises(ConfigError, match="live_trading_confirmed"):
        resolve_trading_mode(secrets(trading_mode="live", **egress_ok()), cfg)


def test_live_env_alone_with_paper_config_is_rejected() -> None:
    with pytest.raises(ConfigError):
        resolve_trading_mode(
            secrets(trading_mode="live", **egress_ok()), config(live_trading_confirmed=True)
        )


def test_live_with_double_confirmation() -> None:
    cfg = config(
        mode="live",
        live_trading_confirmed=True,
        exchanges={"binance": {"enabled": True, "testnet": False}},
    )
    mode = resolve_trading_mode(secrets(trading_mode="live", **egress_ok()), cfg)
    assert mode is Mode.LIVE


def test_confirmed_flag_does_not_enable_live_by_itself() -> None:
    assert resolve_trading_mode(secrets(), config(live_trading_confirmed=True)) is Mode.PAPER


@pytest.mark.parametrize("mode", ["testnet", "live"])
def test_real_api_modes_require_expected_ip(mode: str) -> None:
    cfg = config(
        mode=mode,
        live_trading_confirmed=True,
        exchanges={"binance": {"enabled": True, "testnet": mode == "testnet"}},
    )
    s = secrets(trading_mode=mode, egress_proxy_url=SecretStr("socks5://1.2.3.4:1080"))
    with pytest.raises(ConfigError, match="EGRESS_EXPECTED_IP"):
        resolve_trading_mode(s, cfg)


@pytest.mark.parametrize("ip", ["0.0.0.0", "10.0.0.1", "127.0.0.1", "192.168.1.5", "abc"])
def test_expected_ip_must_be_public(ip: str) -> None:
    s = secrets(
        trading_mode="testnet",
        egress_proxy_url=SecretStr("socks5://1.2.3.4:1080"),
        egress_expected_ip=ip,
    )
    with pytest.raises(ConfigError, match="EGRESS_EXPECTED_IP"):
        resolve_trading_mode(s, config(mode="testnet"))


def test_proxy_mode_requires_proxy_url() -> None:
    s = secrets(trading_mode="testnet", egress_expected_ip=GLOBAL_IP)
    with pytest.raises(ConfigError, match="EGRESS_PROXY_URL"):
        resolve_trading_mode(s, config(mode="testnet", egress={"mode": "proxy"}))


@pytest.mark.parametrize("egress_mode", [EgressMode.DIRECT_VPS, EgressMode.WIREGUARD])
def test_non_proxy_egress_needs_only_expected_ip(egress_mode: EgressMode) -> None:
    s = secrets(trading_mode="testnet", egress_expected_ip=GLOBAL_IP)
    cfg = config(mode="testnet", egress={"mode": egress_mode.value})
    assert resolve_trading_mode(s, cfg) is Mode.TESTNET


def test_testnet_mode_refuses_real_exchange() -> None:
    cfg = config(mode="testnet", exchanges={"binance": {"enabled": True, "testnet": False}})
    with pytest.raises(ConfigError, match="gerçek ağa"):
        resolve_trading_mode(secrets(trading_mode="testnet", **egress_ok()), cfg)


def test_live_mode_refuses_testnet_exchange() -> None:
    cfg = config(mode="live", live_trading_confirmed=True)
    with pytest.raises(ConfigError, match="testnet: true"):
        resolve_trading_mode(secrets(trading_mode="live", **egress_ok()), cfg)


def test_real_api_mode_requires_enabled_exchange() -> None:
    cfg = config(mode="testnet", exchanges={"binance": ExchangeConfig(enabled=False)})
    with pytest.raises(ConfigError, match="borsa"):
        resolve_trading_mode(secrets(trading_mode="testnet", **egress_ok()), cfg)


# --------------------------------------------------------------------- secrets / env file


def test_heartbeat_requires_url() -> None:
    with pytest.raises(ConfigError, match="HEARTBEAT_URL"):
        resolve_trading_mode(secrets(), config(heartbeat={"enabled": True}))
    s = secrets(heartbeat_url=SecretStr("https://hc-ping.com/abc"))
    assert resolve_trading_mode(s, config(heartbeat={"enabled": True})) is Mode.PAPER


def test_load_settings_from_env_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["mode"] = "testnet"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        "TRADING_MODE=testnet\n"
        "EGRESS_PROXY_URL=socks5://user:pass@1.2.3.4:1080\n"
        f"EGRESS_EXPECTED_IP={GLOBAL_IP}\n"
        "BINANCE_API_KEY=\n",
        encoding="utf-8",
    )
    settings = load_settings(cfg_path, env)
    assert settings.mode is Mode.TESTNET
    assert settings.sends_real_orders
    assert settings.secrets.binance_api_key is None  # empty value => not set
    assert "pass" not in repr(settings.secrets)


def test_env_example_parses_as_paper(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "yok.yaml", ROOT / ".env.example")
    assert settings.mode is Mode.PAPER
    assert not settings.sends_real_orders


def test_invalid_env_error_does_not_echo_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TRADING_MODE=supersecretvalue\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_settings(tmp_path / "yok.yaml", env)
    assert "supersecretvalue" not in str(info.value)
