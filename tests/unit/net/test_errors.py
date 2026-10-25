from __future__ import annotations

import pytest

from bot.net.errors import ErrorKind, classify_http_error


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (451, "", ErrorKind.RESTRICTED_LOCATION),
        (
            403,
            '{"code":0,"msg":"Service unavailable from a restricted location"}',
            ErrorKind.RESTRICTED_LOCATION,
        ),
        (
            401,
            '{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}',
            ErrorKind.INVALID_IP,
        ),
        (400, '{"code":-1022,"msg":"Signature for this request is not valid."}', ErrorKind.AUTH),
        (403, "Forbidden", ErrorKind.AUTH),
        (429, "", ErrorKind.RATE_LIMIT),
        (418, "", ErrorKind.RATE_LIMIT),
        (502, "", ErrorKind.SERVER),
        (None, "", ErrorKind.NETWORK),
        (400, '{"code":-1013,"msg":"Filter failure: LOT_SIZE"}', ErrorKind.OTHER),
    ],
)
def test_classify(status: int | None, body: str, kind: ErrorKind) -> None:
    assert classify_http_error(status, body) is kind


def test_kill_switch_relevance() -> None:
    assert ErrorKind.INVALID_IP.counts_for_kill_switch
    assert ErrorKind.RESTRICTED_LOCATION.counts_for_kill_switch
    assert not ErrorKind.RATE_LIMIT.counts_for_kill_switch
    assert not ErrorKind.OTHER.counts_for_kill_switch
