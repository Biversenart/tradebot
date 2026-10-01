"""Binance global (spot + USDⓈ-M futures). Testnet: spot → testnet.binance.vision,
futures → Binance "demo trading" (ccxt dropped the old futures sandbox)."""

from __future__ import annotations

from typing import Any

from bot.exchanges.ccxt_adapter import CcxtAdapter
from bot.exchanges.errors import NotSupportedError


class BinanceAdapter(CcxtAdapter):
    def __init__(self, client: Any, *, market_type: str = "spot", testnet: bool = True) -> None:
        super().__init__("binance", client, market_type=market_type, testnet=testnet)

    @classmethod
    def create(
        cls,
        *,
        market_type: str,
        testnet: bool,
        proxy_options: dict[str, Any],
        api_key: str | None = None,
        secret: str | None = None,
    ) -> BinanceAdapter:
        if market_type not in ("spot", "futures"):
            raise NotSupportedError(f"Binance market_type desteklenmiyor: {market_type}")
        exchange_id = "binanceusdm" if market_type == "futures" else "binance"
        options: dict[str, Any] = {"defaultType": "future" if market_type == "futures" else "spot"}
        client = cls.build_client(
            exchange_id,
            proxy_options=proxy_options,
            api_key=api_key,
            secret=secret,
            options=options,
        )
        if testnet:
            if market_type == "futures":
                client.enable_demo_trading(True)
            else:
                client.set_sandbox_mode(True)
        return cls(client, market_type=market_type, testnet=testnet)
