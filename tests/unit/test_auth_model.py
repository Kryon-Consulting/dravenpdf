"""RenderAuth validation, secret masking, origin matching and per-hop headers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dravenpdf import Cookie, RenderAuth, RequestGuard, StorageState
from dravenpdf.render.auth import MAX_TOTAL_BYTES, canonical_origin, origin_of

SECRET = "TOPSECRET-123"


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("https://Example.COM", "https://example.com"),
        ("https://example.com:443/", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        ("https://example.com:8443", "https://example.com:8443"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_canonical_origin(value: str, canonical: str) -> None:
    assert canonical_origin(value) == canonical


@pytest.mark.parametrize(
    "value",
    ["ftp://example.com", "https://example.com/path", "https://example.com?x=1",
     "https://user:pw@example.com", "example.com", "https://", "https://example.com:99999"],
)  # fmt: skip
def test_invalid_origins(value: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        canonical_origin(value)


@pytest.mark.parametrize(
    ("url", "matches"),
    [
        ("https://api.example.com/v1/data?x=1", True),
        ("https://api.example.com:443/", True),
        ("https://API.example.com/", True),
        ("http://api.example.com/", False),  # scheme
        ("https://api.example.com:8443/", False),  # port
        ("https://example.com/", False),  # parent domain
        ("https://sub.api.example.com/", False),  # subdomain
        ("wss://api.example.com/", False),
    ],
)
def test_headers_only_for_the_exact_origin(url: str, matches: bool) -> None:
    auth = RenderAuth(headers={"https://api.example.com": {"Authorization": SECRET}})

    assert auth.headers_for(url) == ({"authorization": SECRET} if matches else {})


def test_origin_of_non_http() -> None:
    assert origin_of("data:text/plain,x") is None
    assert origin_of("https://a.example/x") == "https://a.example"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"headers": {"https://a.example": {"Cookie": SECRET}}}, "use cookies"),
        ({"headers": {"https://a.example": {"Host": SECRET}}}, "can't be set"),
        ({"headers": {"https://a.example": {"Bad Name": SECRET}}}, "invalid header name"),
        ({"headers": {"https://a.example": {"X": SECRET + "\r\nX-Evil: 1"}}}, "control"),
        ({"headers": {"https://a.example": {"X": "v" * 9000}}}, "longer than"),
        ({"headers": {"https://a.example": {"X": "1", "x": "2"}}}, "twice"),
        ({"headers": {"https://a.example": {}, "https://A.example:443": {}}}, "twice"),
        ({"cookies": [{"name": "s", "value": SECRET}]}, "url or domain"),
        ({"cookies": [{"name": "s;x", "value": SECRET, "url": "https://a.example"}]}, "name"),
        ({"cookies": [{"name": "s", "value": SECRET, "url": "file:///etc/passwd"}]}, "http"),
        ({"storage_state": "/etc/passwd"}, "storage_state"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_validation_errors_never_echo_secrets(data: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message) as info:
        RenderAuth.model_validate(data)

    assert SECRET not in str(info.value)  # what logs and tracebacks show
    # .errors() carries the raw input unless asked not to; the service only ever uses
    # each error's location and message (see server/errors.describe_validation_errors).
    assert SECRET not in repr(info.value.errors(include_input=False))


def test_total_size_limit() -> None:
    big = "v" * 8000
    headers = {f"https://h{i}.example": {"X": big} for i in range(MAX_TOTAL_BYTES // 8000 + 2)}

    with pytest.raises(ValidationError):
        RenderAuth.model_validate({"headers": headers})


def test_secrets_are_masked() -> None:
    auth = RenderAuth(
        cookies=[Cookie(name="s", value=SECRET, url="https://a.example")],
        headers={"https://a.example": {"Authorization": SECRET}},
        storage_state=StorageState.model_validate(
            {
                "origins": [
                    {
                        "origin": "https://a.example",
                        "localStorage": [{"name": "t", "value": SECRET}],
                    }
                ]
            }
        ),
    )

    for text in (repr(auth), str(auth), auth.model_dump_json(), str(auth.model_dump())):
        assert SECRET not in text
    # ...while the browser still gets the real values.
    assert auth.playwright_cookies()[0]["value"] == SECRET
    storage = auth.playwright_storage_state()
    assert storage is not None
    assert storage["origins"][0]["localStorage"][0]["value"] == SECRET


def test_playwright_storage_state_format_is_accepted() -> None:
    # As written by Playwright's context.storage_state(): domain *and* path, extras.
    state = StorageState.model_validate(
        {
            "cookies": [
                {"name": "sid", "value": SECRET, "domain": ".example.com", "path": "/",
                 "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax",
                 "partitionKey": "x"}
            ],
            "origins": [{"origin": "https://app.example.com",
                         "localStorage": [{"name": "token", "value": SECRET}]}],
        }
    )  # fmt: skip
    auth = RenderAuth(storage_state=state)

    (cookie,) = auth.playwright_cookies()
    assert cookie == {
        "name": "sid", "value": SECRET, "domain": ".example.com", "path": "/",
        "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax",
    }  # fmt: skip


# ---------------------------------------------------------------- per-hop headers

PAGE = {"Cookie": "sid=browser", "Authorization": "Bearer page", "Accept": "*/*",
        "Host": "a.example", "Content-Length": "0", "X-Page": "1"}  # fmt: skip


def hops(auth: RenderAuth | None = None) -> RequestGuard:
    return RequestGuard(auth=auth)


def test_first_hop_keeps_the_browser_headers_and_adds_configured() -> None:
    guard = hops(RenderAuth(headers={"https://a.example": {"X-Key": "k"}}))

    headers = guard.hop_headers(PAGE, "https://a.example/x", "https://a.example/x", first_hop=True)

    assert headers == {"cookie": "sid=browser", "authorization": "Bearer page",
                       "accept": "*/*", "x-page": "1", "x-key": "k"}  # fmt: skip


def test_same_origin_redirect_lets_the_jar_supply_cookies() -> None:
    headers = hops().hop_headers(
        PAGE, "https://a.example/next", "https://a.example/x", first_hop=False
    )

    assert "cookie" not in headers
    assert headers["authorization"] == "Bearer page"


def test_cross_origin_redirect_drops_credentials() -> None:
    guard = hops(
        RenderAuth(headers={"https://a.example": {"X-Key": "a"}, "https://b.example": {"X-B": "b"}})
    )

    headers = guard.hop_headers(PAGE, "https://b.example/y", "https://a.example/x", first_hop=False)

    assert headers == {"accept": "*/*", "x-page": "1", "x-b": "b"}


def test_configured_header_overrides_the_page() -> None:
    guard = hops(RenderAuth(headers={"https://a.example": {"Authorization": "Bearer configured"}}))

    headers = guard.hop_headers(PAGE, "https://a.example/", "https://a.example/", first_hop=True)

    assert headers["authorization"] == "Bearer configured"


def test_headers_force_interception() -> None:
    auth = RenderAuth(headers={"https://a.example": {"X": "1"}})

    assert not RequestGuard(allow_private_network=True).checks_requests
    assert RequestGuard(allow_private_network=True, auth=auth).checks_requests
    assert not RequestGuard(allow_private_network=True, auth=RenderAuth()).checks_requests
