"""Rendering pages behind a login, with a real Chromium.

"localhost" and "127.0.0.1" on the same test server are two different origins, which
is how these tests check that one origin's credentials never reach the other.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from urllib.parse import quote

import pytest
from playwright.async_api import Page

from conftest import REQUEST_LOG, Server, pdf_text, requests_tagged
from dravenpdf import (
    AsyncRenderer,
    BlockedRequestError,
    Cookie,
    IncompleteRenderError,
    PdfDocument,
    RenderAuth,
    Renderer,
    RenderOptions,
    StorageState,
)

pytestmark = pytest.mark.browser

READY = RenderOptions(wait_for_ready_flag=True, timeout_ms=15_000)


def text_of(doc: PdfDocument) -> str:
    return " ".join(pdf_text(doc)[0].split())


def origins(server: Server) -> tuple[str, str]:
    """(A, B): the same server as two origins."""
    return server.url("", host="localhost"), server.url("", host="127.0.0.1")


def page_with_images(server: Server, *images: str) -> str:
    query = "&".join(f"img={quote(u, safe='')}" for u in images)
    return server.url(f"/auth/page-with?{query}")


def redirect(server: Server, target: str, host: str = "localhost") -> str:
    return server.url(f"/redirect?to={quote(target, safe='')}", host=host)


@pytest.fixture
async def both_origins() -> AsyncIterator[AsyncRenderer]:
    async with AsyncRenderer(allowed_hosts=["localhost", "127.0.0.1"]) as r:
        yield r


# ---------------------------------------------------------------- logging in


async def test_cookie_protected_page(local_renderer: AsyncRenderer, server: Server) -> None:
    a, _ = origins(server)
    url = server.url("/auth/cookie-page")

    anonymous = await local_renderer.from_url(url)
    by_url = await local_renderer.from_url(
        url, auth=RenderAuth(cookies=[Cookie(name="session", value="s3cret-alice", url=a)])
    )
    by_domain = await local_renderer.from_url(
        url,
        auth=RenderAuth(
            cookies=[Cookie(name="session", value="s3cret-bob", domain="localhost", path="/")]
        ),
    )

    assert "LOGIN REQUIRED" in text_of(anonymous)
    assert "WELCOME alice" in text_of(by_url)
    assert "WELCOME bob" in text_of(by_domain)


async def test_login_kept_in_local_storage(local_renderer: AsyncRenderer, server: Server) -> None:
    a, _ = origins(server)
    # Exactly what Playwright's context.storage_state() produces.
    state = StorageState.model_validate(
        {
            "cookies": [],
            "origins": [{"origin": a, "localStorage": [{"name": "token", "value": "ls-t0ken"}]}],
        }
    )
    url = server.url("/auth/ls-page")

    anonymous = await local_renderer.from_url(url, READY)
    logged_in = await local_renderer.from_url(url, READY, auth=RenderAuth(storage_state=state))

    assert "NO TOKEN" in text_of(anonymous)
    assert "API-OK" in text_of(logged_in)


async def test_headers_reach_same_origin_images_and_api(
    local_renderer: AsyncRenderer, server: Server
) -> None:
    a, _ = origins(server)
    auth = RenderAuth(headers={a: {"X-Tenant": "acme"}})

    doc = await local_renderer.from_url(server.url("/auth/header-page"), READY, auth=auth)

    text = text_of(doc)
    assert "TENANT PAGE" in text
    assert "IMG-OK" in text
    assert "API2-OK" in text
    assert doc.render_report is not None
    assert doc.render_report.ok


def test_sync_renderer_auth(server: Server) -> None:
    a, _ = origins(server)
    auth = RenderAuth(cookies=[Cookie(name="session", value="s3cret-alice", url=a)])

    with Renderer(allowed_hosts=["localhost"]) as r:
        doc = r.from_url(server.url("/auth/cookie-page"), auth=auth)

    assert "WELCOME alice" in text_of(doc)


# ---------------------------------------------------------------- redirects


async def test_credentials_dropped_on_cross_origin_redirect(
    both_origins: AsyncRenderer, server: Server
) -> None:
    a, b = origins(server)
    auth = RenderAuth(
        cookies=[Cookie(name="session", value="s3cret-alice", url=a)],
        headers={a: {"Authorization": "Bearer A-SECRET", "X-Tenant": "acme"}},
    )
    hop = redirect(server, f"{b}/auth/echo?tag=xo1")

    await both_origins.from_url(page_with_images(server, hop), auth=auth)

    first = [h for host, path, h in REQUEST_LOG if path.startswith("/redirect") and "xo1" in path]
    ((host, _, landed),) = requests_tagged("xo1")
    assert first[-1]["authorization"] == "Bearer A-SECRET"  # sent to its own origin...
    assert "s3cret-alice" in first[-1].get("cookie", "")
    assert host.startswith("127.0.0.1")  # ...but not after the redirect to the other one
    assert "authorization" not in landed
    assert "x-tenant" not in landed
    assert "s3cret-alice" not in landed.get("cookie", "")


async def test_second_origin_gets_only_its_own_headers(
    both_origins: AsyncRenderer, server: Server
) -> None:
    a, b = origins(server)
    auth = RenderAuth(headers={a: {"X-Origin-A": "a-secret"}, b: {"X-Origin-B": "b-secret"}})
    direct = f"{b}/auth/echo?tag=so1"
    via_redirect = redirect(server, f"{b}/auth/echo?tag=so2")

    await both_origins.from_url(page_with_images(server, direct, via_redirect), auth=auth)

    page = [h for _, path, h in REQUEST_LOG if path.startswith("/auth/page-with") and "so1" in path]
    assert page[-1]["x-origin-a"] == "a-secret"
    assert "x-origin-b" not in page[-1]
    for tag in ("so1", "so2"):
        ((_, _, received),) = requests_tagged(tag)
        assert received["x-origin-b"] == "b-secret"
        assert "x-origin-a" not in received


async def test_page_set_authorization_is_dropped_cross_origin(
    both_origins: AsyncRenderer, server: Server
) -> None:
    # Same rule for an Authorization header the page's own script sets (as browsers do).
    _, b = origins(server)
    hop = redirect(server, f"{b}/auth/echo?tag=js1")

    async def fetch_with_token(page: Page) -> None:
        await page.evaluate(
            "url => fetch(url, {headers: {Authorization: 'Bearer PAGE-TOKEN'}}).catch(() => 0)",
            hop,
        )

    await both_origins.from_url(server.url("/auth/page-with"), prepare=fetch_with_token)

    first = [h for _, path, h in REQUEST_LOG if path.startswith("/redirect") and "js1" in path]
    ((_, _, landed),) = requests_tagged("js1")
    assert first[-1]["authorization"] == "Bearer PAGE-TOKEN"
    assert "authorization" not in landed


async def test_blocked_redirect_stays_blocked(
    local_renderer: AsyncRenderer, server: Server
) -> None:
    # Configuring headers for an origin doesn't allow it: 127.0.0.1 is still private.
    a, b = origins(server)
    auth = RenderAuth(headers={a: {"X-Tenant": "acme"}, b: {"X-Tenant": "acme"}})
    hop = redirect(server, f"{b}/auth/echo?tag=bl1")

    with pytest.raises(BlockedRequestError, match="redirected from"):
        await local_renderer.from_url(page_with_images(server, hop), auth=auth)

    assert requests_tagged("bl1") == []


# ---------------------------------------------------------------- isolation


async def test_renders_are_isolated(local_renderer: AsyncRenderer, server: Server) -> None:
    a, _ = origins(server)
    cookie_page = server.url("/auth/cookie-page")

    def as_user(secret: str) -> RenderAuth:
        return RenderAuth(
            cookies=[Cookie(name="session", value=secret, url=a)],
            headers={a: {"X-Tenant": "acme"}},
        )

    alice, bob = await asyncio.gather(
        local_renderer.from_url(cookie_page, auth=as_user("s3cret-alice")),
        local_renderer.from_url(cookie_page, auth=as_user("s3cret-bob")),
    )
    later_cookie = await local_renderer.from_url(cookie_page)
    later_header = await local_renderer.from_url(server.url("/auth/header-page"))

    assert "WELCOME alice" in text_of(alice)
    assert "WELCOME bob" in text_of(bob)
    assert "LOGIN REQUIRED" in text_of(later_cookie)
    assert "LOGIN REQUIRED" in text_of(later_header)


# ---------------------------------------------------------------- secrets stay secret


async def test_secrets_never_reach_logs_errors_or_reports(
    server: Server, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    a, _ = origins(server)
    secret = "TOP-SECRET-VALUE-7f3a"
    dead = "http://localhost:1"  # nothing listens: the guard's own fetch fails
    auth = RenderAuth(
        cookies=[Cookie(name="session", value=secret, url=a)],
        headers={a: {"Authorization": f"Bearer {secret}"}, dead: {"X-Key": secret}},
    )

    async with AsyncRenderer(allowed_hosts=["localhost"]) as r:
        lenient = await r.from_url(page_with_images(server, f"{dead}/x.png"), auth=auth)
        with pytest.raises(IncompleteRenderError) as strict:
            await r.from_url(
                page_with_images(server, f"{dead}/x.png"),
                RenderOptions(fail_on_resource_errors=True),
                auth=auth,
            )

    assert lenient.render_report is not None
    assert not lenient.render_report.ok  # the failure was seen...
    for text in (
        caplog.text,  # ...but its Playwright call log (which lists headers) was not logged
        strict.value.message,
        str(strict.value),
        strict.value.report.summary(),
        repr(lenient.render_report),
        repr(auth),
        str(auth),
        auth.model_dump_json(),
    ):
        assert secret not in text
