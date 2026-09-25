"""Rendering pages behind a login, with a real Chromium.

"localhost" and "127.0.0.1" on the same test server are two different origins, which
is how these tests check that one origin's credentials never reach the other.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
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


# ---------------------------------------------------------------- every render source

SOURCE_PAGE = """<p>SOURCE PAGE</p>
<img src="{img}" onload="document.body.append(' IMG-OK')"
     onerror="document.body.append(' IMG-FAIL')">
<img src="{other}">
<p id="api"></p><script>
fetch('{api}').then(r => r.text()).catch(() => 'API-FAILED').then(t => {{
  document.getElementById('api').textContent = t; window.__DRAVENPDF_READY__ = true; }});
</script>"""


def source_page(server: Server, tag: str) -> str:
    """Loads a header-protected image and API on A, and an image on B."""
    return SOURCE_PAGE.format(
        img=server.url(f"/auth/img.svg?tag={tag}"),
        api=server.url(f"/auth/cors-api?tag={tag}"),
        other=server.url(f"/auth/echo?tag={tag}-other", host="127.0.0.1"),
    )


async def render_source(
    renderer: AsyncRenderer, source: str, html: str, tmp_path: Path, auth: RenderAuth | None
) -> PdfDocument:
    if source == "html":
        return await renderer.from_html(html, READY, auth=auth)
    if source == "html_base_url":
        return await renderer.from_html(html, READY, base_url="http://localhost/", auth=auth)
    if source == "file":
        (tmp_path / "page.html").write_text(html)
        return await renderer.from_file(tmp_path / "page.html", READY, auth=auth)
    if source == "template":
        return await renderer.from_template("{{ note }}" + html, {"note": "T"}, READY, auth=auth)
    if source == "template_dir":
        (tmp_path / "page.html").write_text("{{ note }}" + html)
        return await renderer.from_template(
            "page.html", {"note": "T"}, READY, template_dir=tmp_path, auth=auth
        )
    if source == "bundle":
        return await renderer.from_html(html, READY, assets={"x.css": b""}, auth=auth)
    assert source == "bundle_template"
    return await renderer.from_template(
        "{{ note }}" + html, {"note": "T"}, READY, assets={"x.css": b""}, auth=auth
    )


SOURCES = ["html", "html_base_url", "file", "template", "template_dir", "bundle",
           "bundle_template"]  # fmt: skip


@pytest.mark.parametrize("source", SOURCES)
async def test_every_source_sends_headers_to_their_origin_only(
    both_origins: AsyncRenderer, server: Server, tmp_path: Path, source: str
) -> None:
    a, _ = origins(server)
    auth = RenderAuth(headers={a: {"X-Tenant": "acme"}})

    anonymous = await render_source(
        both_origins, source, source_page(server, f"anon-{source}"), tmp_path, None
    )
    logged_in = await render_source(
        both_origins, source, source_page(server, f"src-{source}"), tmp_path, auth
    )

    # The image answers 401 with a picture either way, so check what was sent.
    assert "API-DENIED" in text_of(anonymous)
    assert "CORS-API-OK" in text_of(logged_in)
    anonymous_to_a = [h for _, p, h in requests_tagged(f"anon-{source}") if "-other" not in p]
    assert len(anonymous_to_a) == 2
    assert not any("x-tenant" in h for h in anonymous_to_a)
    to_a = [h for _, path, h in requests_tagged(f"src-{source}") if "-other" not in path]
    (to_b,) = [h for _, _, h in requests_tagged(f"src-{source}-other")]
    assert len(to_a) == 2
    assert all(h.get("x-tenant") == "acme" for h in to_a)
    assert "x-tenant" not in to_b


def test_sync_renderer_auth_for_html(server: Server) -> None:
    a, _ = origins(server)

    with Renderer(allowed_hosts=["localhost", "127.0.0.1"]) as r:
        doc = r.from_html(
            source_page(server, "sync-html"),
            READY,
            auth=RenderAuth(headers={a: {"X-Tenant": "acme"}}),
        )

    assert "CORS-API-OK" in text_of(doc)


@pytest.mark.parametrize("source", ["html", "file", "bundle"])
async def test_headers_dropped_on_redirect_from_any_source(
    both_origins: AsyncRenderer, server: Server, tmp_path: Path, source: str
) -> None:
    a, b = origins(server)
    tag = f"rd-{source}"
    hop = redirect(server, f"{b}/auth/echo?tag={tag}")
    auth = RenderAuth(headers={a: {"Authorization": "Bearer s3cret", "X-Tenant": "acme"}})
    html = f'<img src="{hop}"><p>PAGE</p>'

    if source == "html":
        await both_origins.from_html(html, auth=auth)
    elif source == "file":
        (tmp_path / "p.html").write_text(html)
        await both_origins.from_file(tmp_path / "p.html", auth=auth)
    else:
        await both_origins.from_html(html, assets={}, auth=auth)

    (final,) = [h for _, _, h in requests_tagged(tag)]
    assert "authorization" not in final
    assert "x-tenant" not in final
    (first,) = [h for _, path, h in REQUEST_LOG if path.startswith("/redirect") and tag in path]
    assert first.get("x-tenant") == "acme"


async def test_blocked_host_from_html_gets_nothing(
    local_renderer: AsyncRenderer, server: Server
) -> None:
    _, b = origins(server)  # 127.0.0.1 isn't allowed for local_renderer
    auth = RenderAuth(headers={b: {"Authorization": "Bearer s3cret"}})

    with pytest.raises(BlockedRequestError):
        await local_renderer.from_html(f'<img src="{b}/auth/echo?tag=bl-html">', auth=auth)

    assert requests_tagged("bl-html") == []


# ---------------------------------------------------------------- cookies and SameSite


async def test_cookies_follow_chromium_samesite_rules(
    both_origins: AsyncRenderer, server: Server
) -> None:
    a, _ = origins(server)
    lax = RenderAuth(cookies=[Cookie(name="session", value="s3cret-alice", url=a)])  # default Lax
    strict = RenderAuth(
        cookies=[Cookie(name="session", value="s3cret-alice", url=a, same_site="Strict")]
    )

    def image_on_a(tag: str) -> str:
        return f"{a}/auth/echo?tag={tag}"

    await both_origins.from_url(page_with_images(server, image_on_a("ss-same")), auth=strict)
    await both_origins.from_url(
        server.url(
            f"/auth/page-with?img={quote(image_on_a('ss-cross'), safe='')}", host="127.0.0.1"
        ),
        auth=strict,
    )
    await both_origins.from_html(f'<img src="{image_on_a("ss-html")}">', auth=lax)

    def cookie(tag: str) -> str:
        (headers,) = [h for _, path, h in requests_tagged(tag) if path.startswith("/auth/echo")]
        return headers.get("cookie", "")

    assert cookie("ss-same") == "session=s3cret-alice"  # a page on the same site
    # A page on another site (127.0.0.1, or about:blank for from_html) gets what Chrome
    # sends: no Lax or Strict cookies. The guard must not add them from the cookie store.
    assert cookie("ss-cross") == ""
    assert cookie("ss-html") == ""


# ---------------------------------------------------------------- IndexedDB


async def indexed_db_login(renderer: AsyncRenderer, server: Server, token: str) -> RenderAuth:
    """Log in by storing ``token`` in IndexedDB, then capture Playwright's snapshot."""
    async with renderer.pool.context() as ctx:
        page = await ctx.new_page()
        await page.goto(server.url(f"/auth/idb-write?token={token}"))
        await page.wait_for_function("window.__DRAVENPDF_READY__ === true")
        state = await ctx.storage_state(indexed_db=True)
    assert state["origins"][0]["indexedDB"]
    return RenderAuth(storage_state=StorageState.model_validate(state))


async def test_login_kept_in_indexed_db(local_renderer: AsyncRenderer, server: Server) -> None:
    alice = await indexed_db_login(local_renderer, server, "s3cret-idb-alice")
    bob = await indexed_db_login(local_renderer, server, "s3cret-idb-bob")
    url = server.url("/auth/idb-page")

    as_alice, as_bob, anonymous = await asyncio.gather(
        local_renderer.from_url(url, READY, auth=alice),
        local_renderer.from_url(url, READY, auth=bob),
        local_renderer.from_url(url, READY),
    )
    later = await local_renderer.from_url(url, READY)

    assert "TOKEN s3cret-idb-alice" in text_of(as_alice)  # on the very first navigation
    assert "TOKEN s3cret-idb-bob" in text_of(as_bob)
    assert "NO TOKEN" in text_of(anonymous)
    assert "NO TOKEN" in text_of(later)
    for text in (repr(alice), str(alice), alice.model_dump_json()):
        assert "s3cret-idb-alice" not in text
