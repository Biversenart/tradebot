"""BTCTurk (TRY pairs). No testnet and no ccxt.pro websocket: streams are REST-polled."""

from __future__ import annotations

from typing import Any

from bot.core.models import Order, OrderRequest
from bot.exchanges.ccxt_adapter import CcxtAdapter
from bot.exchanges.errors import NotSupportedError


class BtcturkAdapter(CcxtAdapter):
    poll_interval = 2.0

    def __init__(self, client: Any, *, testnet: bool = False) -> None:
        super().__init__("btcturk", client, market_type="spot", testnet=testnet, supports_ws=False)

    @classmethod
    def create(
        cls,
        *,
        testnet: bool,
        proxy_options: dict[str, Any],
        api_key: str | None = None,
        secret: str | None = None,
    ) -> BtcturkAdapter:
        # BTCTurk has no testnet: in testnet mode no credentials are attached at all.
        client = cls.build_client(
            "btcturk",
            proxy_options=proxy_options,
            api_key=None if testnet else api_key,
            secret=None if testnet else secret,
            pro=False,
        )
        return cls(client, testnet=testnet)

    async def create_order(self, request: OrderRequest) -> Order:
        if self.testnet:
            raise NotSupportedError(
                "BTCTurk'ün test ağı yok; testnet modunda BTCTurk'e emir gönderilmez."
            )
        return await super().create_order(request)
