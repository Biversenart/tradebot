"""Exchange API credentials from the environment / `.env` (CLAUDE.md rule 2).

Naming, for any supported exchange `<NAME>` (upper-case config key):
    <NAME>_API_KEY, <NAME>_API_SECRET, <NAME>_API_PASSWORD   (live)
    <NAME>_TESTNET_API_KEY, <NAME>_TESTNET_API_SECRET, <NAME>_TESTNET_API_PASSWORD
`_API_PASSWORD` is the API passphrase some exchanges require (OKX, KuCoin, Bitget).
Process environment wins over `.env` (Docker passes `.env` as environment). Values are kept
as SecretStr and never logged.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr


@dataclass(frozen=True)
class ExchangeKeys:
    api_key: SecretStr
    secret: SecretStr
    password: SecretStr | None = None

    def __repr__(self) -> str:  # never show values
        return "ExchangeKeys(***)"


def env_names(name: str, testnet: bool) -> tuple[str, str, str]:
    base = f"{name.upper()}_{'TESTNET_' if testnet else ''}API_"
    return base + "KEY", base + "SECRET", base + "PASSWORD"


def load_exchange_keys(
    names: Iterable[str], env_file: Path | None = Path(".env")
) -> dict[tuple[str, bool], ExchangeKeys]:
    file_values: dict[str, str | None] = {}
    if env_file is not None and env_file.exists():
        file_values = dict(dotenv_values(env_file))

    def get(key: str) -> str | None:
        raw = os.environ.get(key)
        if raw is None:
            raw = file_values.get(key)
        return raw.strip() if raw and raw.strip() else None

    out: dict[tuple[str, bool], ExchangeKeys] = {}
    for name in names:
        for testnet in (False, True):
            k, s, p = (get(n) for n in env_names(name, testnet))
            if k and s:
                out[(name, testnet)] = ExchangeKeys(
                    SecretStr(k), SecretStr(s), SecretStr(p) if p else None
                )
    return out
