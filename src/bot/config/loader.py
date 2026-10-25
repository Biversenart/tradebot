"""Load config + secrets and resolve the effective trading mode.

Safety rules enforced here (CLAUDE.md rules 1 and 8):
- `live` requires BOTH `TRADING_MODE=live` in the environment AND
  `live_trading_confirmed: true` in config.yaml (and config `mode: live`).
- `.env` TRADING_MODE and config `mode` must agree when both are set.
- `testnet`/`live` require a fixed egress IP (and a proxy URL for proxy modes).
- `testnet` mode never talks to a real exchange; `live` never to a testnet.
Never bypass or shorten these checks.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from bot.config.schema import AppConfig, Mode
from bot.config.secrets import Secrets


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class Settings:
    config: AppConfig
    secrets: Secrets
    mode: Mode

    @property
    def sends_real_orders(self) -> bool:
        """True when orders reach an exchange API (testnet or live)."""
        return self.mode in (Mode.TESTNET, Mode.LIVE)


def load_app_config(path: Path) -> AppConfig:
    """Load YAML config. A missing file yields safe (paper) defaults."""
    if not path.exists():
        return AppConfig()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config dosyası okunamadı ({path}): {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config dosyası bir sözlük olmalı: {path}")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Config doğrulanamadı ({path}):\n{exc}") from exc


def _validate_expected_ip(value: str | None, mode: Mode) -> None:
    if not value:
        raise ConfigError(f"{mode} modu için EGRESS_EXPECTED_IP zorunludur.")
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ConfigError(f"EGRESS_EXPECTED_IP geçerli bir IP değil: {value!r}") from exc
    if not ip.is_global:
        raise ConfigError(
            f"EGRESS_EXPECTED_IP genel (public) bir IP olmalı; {value!r} kabul edilmez."
        )


def resolve_trading_mode(secrets: Secrets, config: AppConfig) -> Mode:
    """Return the effective trading mode, enforcing all safety checks."""
    env_mode = secrets.trading_mode
    if env_mode is not None and env_mode is not config.mode:
        raise ConfigError(
            f".env TRADING_MODE={env_mode} ile config.yaml mode={config.mode} uyuşmuyor. "
            "İkisini aynı değere getirin."
        )
    mode = config.mode

    if mode is Mode.LIVE and (env_mode is not Mode.LIVE or not config.live_trading_confirmed):
        raise ConfigError(
            "Canlı işlem için .env'de TRADING_MODE=live VE config.yaml'da "
            "live_trading_confirmed: true birlikte gerekli. Canlı işlem açılmadı."
        )

    if config.heartbeat.enabled and secrets.heartbeat_url is None:
        raise ConfigError("heartbeat.enabled=true iken HEARTBEAT_URL zorunludur.")

    if mode in (Mode.TESTNET, Mode.LIVE):
        _validate_expected_ip(secrets.egress_expected_ip, mode)
        if config.egress.mode.requires_proxy_url and secrets.egress_proxy_url is None:
            raise ConfigError(f"egress.mode={config.egress.mode} için EGRESS_PROXY_URL zorunludur.")
        enabled = config.enabled_exchanges
        if not enabled:
            raise ConfigError(f"{mode} modunda en az bir borsa etkin olmalı.")
        for name, ex in enabled.items():
            if mode is Mode.TESTNET and not ex.testnet:
                raise ConfigError(
                    f"testnet modunda '{name}' gerçek ağa (testnet: false) bağlanamaz."
                )
            if mode is Mode.LIVE and ex.testnet:
                raise ConfigError(f"live modunda '{name}' için testnet: true olamaz.")
    return mode


def load_settings(config_path: Path, env_file: Path | None = Path(".env")) -> Settings:
    config = load_app_config(config_path)
    try:
        secrets = Secrets(_env_file=env_file)
    except ValidationError as exc:
        # Never echo input values: they may be secrets.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f".env doğrulanamadı: {problems}") from exc
    mode = resolve_trading_mode(secrets, config)
    return Settings(config=config, secrets=secrets, mode=mode)
