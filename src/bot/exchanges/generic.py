"""Profile-driven ccxt adapter for every exchange without a dedicated adapter (spot only).

- Testnet: `set_sandbox_mode(True)` when the profile has sandbox/demo endpoints; otherwise
  testnet mode connects WITHOUT keys and refuses orders (public data only), like BTCTurk.
- Stop orders: sent with the profile's ccxt parameter (`stopLossPrice` / `triggerPrice`);
  exchanges without exchange-side stops raise NotSupportedError (rule 9 — the coordinator
  never opens positions there in the first place).
- Client order ids: our ids (`tb` + 30 hex) are converted to the exchange's format
  deterministically (Gate `t-` prefix, UUID for Kraken/Bitvavo, integer for Bitfinex), so a
  retry after a network error reuses the same id (idempotency) and lookups work.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from bot.core.models import Order, OrderRequest, OrderType
from bot.exchanges.ccxt_adapter import CcxtAdapter
from bot.exchanges.errors import NotSupportedError, OrderNotFoundError
from bot.exchanges.profiles import ExchangeProfile

_NS = uuid.UUID("6f1c3f0e-4a54-4f7e-9a1d-2b6c7d7e8f90")


def exchange_client_id(our_id: str, fmt: str) -> str:
    if fmt == "gate":
        return "t-" + our_id[:28]
    if fmt == "uuid":
        return str(uuid.uuid5(_NS, our_id))
    if fmt == "int":
        digest = hashlib.sha256(our_id.encode()).digest()
        return str(int.from_bytes(digest[:5], "big"))  # < 2^40, fits Bitfinex cid
    return our_id


class GenericCcxtAdapter(CcxtAdapter):
    def __init__(self, profile: ExchangeProfile, client: Any, *, testnet: bool = False) -> None:
        super().__init__(
            profile.name, client, market_type="spot", testnet=testnet, supports_ws=profile.websocket
        )
        self.profile = profile
        self.supports_exchange_stop = profile.exchange_stop
        self._ids: dict[str, str] = {}  # exchange client id -> our client id

    @classmethod
    def create(
        cls,
        profile: ExchangeProfile,
        *,
        market_type: str,
        testnet: bool,
        proxy_options: dict[str, Any],
        api_key: str | None = None,
        secret: str | None = None,
        password: str | None = None,
    ) -> GenericCcxtAdapter:
        if market_type != "spot":
            raise NotSupportedError(
                f"{profile.label}: bu borsada yalnızca spot (futures yalnızca Binance)."
            )
        use_keys = not testnet or profile.sandbox
        client = cls.build_client(
            profile.ccxt_id,
            proxy_options=proxy_options,
            api_key=api_key if use_keys else None,
            secret=secret if use_keys else None,
            options={"defaultType": "spot"},
            pro=profile.websocket,
        )
        if use_keys and password:
            client.password = password
        if testnet and profile.sandbox:
            client.set_sandbox_mode(True)
        return cls(profile, client, testnet=testnet)

    # ---------------------------------------------------------------- client ids
    def _ex_id(self, our_id: str) -> str:
        ex = exchange_client_id(our_id, self.profile.client_id)
        self._ids[ex] = our_id
        return ex

    def _order(self, o: dict[str, Any]) -> Order:
        cid = o.get("clientOrderId")
        if cid is not None and str(cid) in self._ids:
            o = {**o, "clientOrderId": self._ids[str(cid)]}
        return super()._order(o)

    # ---------------------------------------------------------------- orders
    def order_params(self, request: OrderRequest) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {"clientOrderId": self._ex_id(request.client_order_id)}
        if request.order_type is OrderType.MARKET:
            return "market", params
        if request.order_type is OrderType.LIMIT:
            return "limit", params
        if not self.profile.exchange_stop:
            raise NotSupportedError(
                f"{self.profile.label} borsada stop emri desteklemiyor (kural 9: pozisyon açılmaz)."
            )
        params[self.profile.stop_param] = float(request.stop_price or 0)
        return ("limit" if request.order_type is OrderType.STOP_LIMIT else "market"), params

    async def create_order(self, request: OrderRequest) -> Order:
        if self.testnet and not self.profile.sandbox:
            raise NotSupportedError(
                f"{self.profile.label} test ağı sunmuyor; testnet modunda emir gönderilmez."
            )
        return await super().create_order(request)

    async def fetch_order_by_client_id(self, client_order_id: str, symbol: str) -> Order:
        """Look the order up by our id among open, then closed orders (no exchange-specific
        lookup parameter needed)."""
        target = exchange_client_id(client_order_id, self.profile.client_id)
        self._ids[target] = client_order_id
        sources = ["fetch_open_orders"]
        has = getattr(self._client, "has", {}) or {}
        if has.get("fetchClosedOrders"):
            sources.append("fetch_closed_orders")
        for method in sources:
            try:
                rows = await self._call(getattr(self._client, method), symbol)
            except NotSupportedError:
                continue
            for raw in rows:
                if str(raw.get("clientOrderId")) in (target, client_order_id):
                    raw.setdefault("symbol", symbol)
                    return self._order(raw)
        raise OrderNotFoundError(f"{client_order_id} bulunamadı ({self.profile.label})")
