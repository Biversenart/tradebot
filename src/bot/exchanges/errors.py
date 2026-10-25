"""Exchange-agnostic errors raised by adapters (ccxt exceptions never leak upward)."""

from __future__ import annotations

from bot.net.errors import ErrorKind


class ExchangeAdapterError(RuntimeError):
    kind: ErrorKind = ErrorKind.OTHER

    def __init__(self, message: str, kind: ErrorKind | None = None) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind


class ExchangeNetworkError(ExchangeAdapterError):
    kind = ErrorKind.NETWORK


class RateLimitError(ExchangeAdapterError):
    kind = ErrorKind.RATE_LIMIT


class AuthError(ExchangeAdapterError):
    kind = ErrorKind.AUTH


class InvalidIpError(AuthError):
    kind = ErrorKind.INVALID_IP


class RestrictedLocationError(ExchangeAdapterError):
    kind = ErrorKind.RESTRICTED_LOCATION


class ExchangeUnavailableError(ExchangeAdapterError):
    kind = ErrorKind.SERVER


class InsufficientFundsError(ExchangeAdapterError):
    pass


class InvalidOrderError(ExchangeAdapterError):
    pass


class OrderNotFoundError(ExchangeAdapterError):
    pass


class NotSupportedError(ExchangeAdapterError):
    pass
