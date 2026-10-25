"""Classification of network / exchange errors relevant to the egress guard and kill switch."""

from __future__ import annotations

import re
from enum import StrEnum


class EgressError(RuntimeError):
    """Base class for egress problems. Never handled by falling back to a direct connection."""


class EgressMisconfiguredError(EgressError):
    """Egress settings are missing or invalid."""


class EgressUnavailableError(EgressError):
    """The proxy / tunnel could not be reached. Requests are NOT retried directly."""


class EgressBlockedError(EgressError):
    """Orders are blocked because the external IP is not verified."""


class ErrorKind(StrEnum):
    RESTRICTED_LOCATION = "restricted_location"  # HTTP 451 / 403 "restricted location"
    INVALID_IP = "invalid_ip"  # Binance -2015 etc. (IP not whitelisted)
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    NETWORK = "network"
    OTHER = "other"

    @property
    def counts_for_kill_switch(self) -> bool:
        return self in (
            ErrorKind.RESTRICTED_LOCATION,
            ErrorKind.INVALID_IP,
            ErrorKind.AUTH,
            ErrorKind.SERVER,
            ErrorKind.NETWORK,
        )


_RESTRICTED = re.compile(r"restricted location|eligibility|unavailable in your region", re.I)
_INVALID_IP = re.compile(r"-2015|invalid api-key, ip|ip.*not.*(allowed|whitelist)", re.I)
_AUTH = re.compile(r"-2014|-1022|invalid api|signature", re.I)


def classify_http_error(status: int | None, body: str = "") -> ErrorKind:
    """Classify an exchange HTTP response (status may be None for transport errors)."""
    if status is None:
        return ErrorKind.NETWORK
    if status == 451 or (status == 403 and _RESTRICTED.search(body)):
        return ErrorKind.RESTRICTED_LOCATION
    if _INVALID_IP.search(body):
        return ErrorKind.INVALID_IP
    if status in (429, 418):
        return ErrorKind.RATE_LIMIT
    if status in (401, 403) or (400 <= status < 500 and _AUTH.search(body)):
        return ErrorKind.AUTH
    if status >= 500:
        return ErrorKind.SERVER
    return ErrorKind.OTHER
