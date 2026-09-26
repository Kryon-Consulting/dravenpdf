"""Unguarded Chromium's on-wire cookie decisions on local HTTPS sites."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from playwright.async_api import BrowserContext, Page

from dravenpdf import AsyncRenderer, Cookie, RenderAuth
from dravenpdf.render._proxy_gate import ProxyGate
from dravenpdf.render.assets import ORIGIN as BUNDLE_ORIGIN
from dravenpdf.render.pool import DEFAULT_LAUNCH_ARGS, BrowserPool
from https_fixture import HttpsSites

pytestmark = pytest.mark.browser

SOURCES = (
    "url",
    "html",
    "html_base_url",
    "file",
    "template_source",
    "template_dir",
    "bundle",
    "bundle_template",
)


async def set_third_party_cookie_restriction(page: Page, enabled: bool) -> None:
    """Set test-only Chromium policy before the page navigates."""
    session = await page.context.new_cdp_session(page)
    await session.send("Network.enable")
    await session.send(
        "Network.setCookieControls",
        {
            "enableThirdPartyCookieRestriction": enabled,
        },
    )


@asynccontextmanager
async def guarded_https_pool(
    sites: HttpsSites, monkeypatch: pytest.MonkeyPatch, blocked: bool
) -> AsyncIterator[BrowserPool]:
    start = ProxyGate.start.__func__

    async def with_test_ca(cls: type[ProxyGate], *args: object) -> ProxyGate:
        return await start(cls, *args, upstream_ca=sites.upstream_ca)

    monkeypatch.setattr(ProxyGate, "start", classmethod(with_test_ca))
    args = (*DEFAULT_LAUNCH_ARGS, f"--ignore-certificate-errors-spki-list={sites.spki_hash}")
    async with BrowserPool(launch_args=args) as pool:
        new_context = pool._new_context

        async def configured_context(*args: object) -> BrowserContext:
            context = await new_context(*args)
            await context.grant_permissions(["local-network-access"], origin=BUNDLE_ORIGIN)
            new_page = context.new_page

            async def configured_page() -> Page:
                page = await new_page()
                await set_third_party_cookie_restriction(page, blocked)
                return page

            context.new_page = configured_page
            return context

        monkeypatch.setattr(pool, "_new_context", configured_context)
        yield pool


async def load_test_source(page: Page, source: str, sites: HttpsSites, folder: Path) -> None:
    html = "<p>source</p>"
    if source == "url":
        await page.goto(f"{sites.loopback_origin}/secret")
    elif source == "html_base_url":
        await page.set_content(f'<base href="{sites.loopback_origin}/">' + html)
    elif source in {"html", "template_source"}:
        await page.set_content(html)
    elif source == "file":
        await page.goto((folder / "file.html").as_uri())
    elif source == "template_dir":
        await page.goto(folder.as_uri() + "/")
        await page.set_content(html)
    else:
        assert source in {"bundle", "bundle_template"}
        await page.goto(BUNDLE_ORIGIN + "/")


async def render_test_source(
    renderer: AsyncRenderer,
    source: str,
    sites: HttpsSites,
    folder: Path,
    auth: RenderAuth,
    prepare: object,
) -> None:
    html = "<p>source</p>"
    if source == "url":
        await renderer.from_url(f"{sites.loopback_origin}/secret", auth=auth, prepare=prepare)
    elif source == "html":
        await renderer.from_html(html, auth=auth, prepare=prepare)
    elif source == "html_base_url":
        await renderer.from_html(
            html, base_url=sites.loopback_origin + "/", auth=auth, prepare=prepare
        )
    elif source == "file":
        await renderer.from_file(folder / "file.html", auth=auth, prepare=prepare)
    elif source == "template_source":
        await renderer.from_template("{{ note }}", {"note": "source"}, auth=auth, prepare=prepare)
    elif source == "template_dir":
        await renderer.from_template(
            "page.html", {"note": "source"}, template_dir=folder, auth=auth, prepare=prepare
        )
    elif source == "bundle":
        await renderer.from_html(html, assets={}, auth=auth, prepare=prepare)
    else:
        assert source == "bundle_template"
        await renderer.from_template(
            "{{ note }}", {"note": "source"}, assets={}, auth=auth, prepare=prepare
        )


@pytest.mark.parametrize("blocked", [False, True], ids=["allowed", "blocked"])
async def test_chromium_cross_site_cookie_baseline(
    https_sites: HttpsSites, test_browser_pool: BrowserPool, blocked: bool
) -> None:
    sites = https_sites
    tag = "third-party-blocked" if blocked else "cross-none"
    async with test_browser_pool.context() as context:
        await context.add_cookies(
            [
                {
                    "name": "strict",
                    "value": "private",
                    "url": sites.b_origin,
                    "sameSite": "Strict",
                    "secure": True,
                },
                {
                    "name": "lax",
                    "value": "private",
                    "url": sites.b_origin,
                    "sameSite": "Lax",
                    "secure": True,
                },
                {
                    "name": "none",
                    "value": "allowed",
                    "url": sites.b_origin,
                    "sameSite": "None",
                    "secure": True,
                },
            ]
        )
        page = await context.new_page()
        await set_third_party_cookie_restriction(page, blocked)
        await page.goto(f"{sites.a_origin}/secret")
        await page.evaluate(
            """async ({origin, tag}) => {
            await Promise.all([
                new Promise((resolve, reject) => {
                    const image = new Image();
                    image.onload = resolve;
                    image.onerror = reject;
                    image.src = `${origin}/img.svg?tag=${tag}-image`;
                    document.body.append(image);
                }),
                fetch(`${origin}/auth/cors-api?tag=${tag}-fetch`, {credentials: 'include'}),
            ]);
        }""",
            {"origin": sites.b_origin, "tag": tag},
        )

    (image,) = sites.received(f"{tag}-image")
    (fetch,) = sites.received(f"{tag}-fetch")
    assert image.host.startswith("b.test:")
    assert fetch.host.startswith("b.test:")
    for request, path in (
        (image, f"/img.svg?tag={tag}-image"),
        (fetch, f"/auth/cors-api?tag={tag}-fetch"),
    ):
        assert request.method == "GET"
        assert request.path == path
        cookie = request.headers.get("cookie", "")
        if blocked:
            assert cookie == "", request
        else:
            assert "none=allowed" in cookie, request
            assert "strict=" not in cookie
            assert "lax=" not in cookie


async def test_http_peer_is_recorded(
    https_sites: HttpsSites, test_browser_pool: BrowserPool
) -> None:
    async with test_browser_pool.context() as context:
        page = await context.new_page()
        await page.goto(f"{https_sites.http_origin}/secret?tag=http-peer")

    (request,) = https_sites.received("http-peer")
    assert request.method == "GET"
    assert request.path == "/secret?tag=http-peer"
    assert request.host == https_sites.http_origin.removeprefix("http://")
    assert "host" in request.headers


async def test_guarded_https_top_level_cookie_uses_validated_upstream(
    https_sites: HttpsSites, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = ProxyGate.start.__func__

    async def with_test_ca(cls: type[ProxyGate], *args: object) -> ProxyGate:
        return await start(cls, *args, upstream_ca=https_sites.upstream_ca)

    monkeypatch.setattr(ProxyGate, "start", classmethod(with_test_ca))
    args = (
        *DEFAULT_LAUNCH_ARGS,
        f"--ignore-certificate-errors-spki-list={https_sites.spki_hash}",
    )
    async with (
        BrowserPool(launch_args=args) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):
        await renderer.from_url(
            f"{https_sites.ip_origin}/secret?tag=guarded-top",
            auth=RenderAuth(
                cookies=[
                    Cookie(
                        name="session",
                        value="guarded",
                        url=https_sites.ip_origin,
                        secure=True,
                        same_site="None",
                    )
                ]
            ),
        )
    (request,) = https_sites.received("guarded-top")
    assert request.headers.get("cookie") == "session=guarded"


@pytest.mark.parametrize("blocked", [False, True], ids=["allowed", "blocked"])
async def test_guarded_cross_site_image_cookies_match_chromium(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
    blocked: bool,
) -> None:
    sites = https_sites
    cookies = [
        {
            "name": name,
            "value": "value",
            "url": sites.ip_origin,
            "sameSite": same_site,
            "secure": True,
        }
        for name, same_site in (("strict", "Strict"), ("lax", "Lax"), ("none", "None"))
    ]

    async with test_browser_pool.context() as context:
        await context.add_cookies(cookies)
        page = await context.new_page()
        await set_third_party_cookie_restriction(page, blocked)
        await page.goto(f"{sites.loopback_origin}/secret")
        await page.evaluate(
            """url => new Promise(resolve => {
                const img = new Image();
                img.onload = resolve;
                img.onerror = resolve;
                img.src = url;
                document.body.append(img);
            })""",
            f"{sites.ip_origin}/img.svg?tag=baseline-{blocked}",
        )

    start = ProxyGate.start.__func__

    async def with_test_ca(cls: type[ProxyGate], *args: object) -> ProxyGate:
        return await start(cls, *args, upstream_ca=sites.upstream_ca)

    monkeypatch.setattr(ProxyGate, "start", classmethod(with_test_ca))
    args = (*DEFAULT_LAUNCH_ARGS, f"--ignore-certificate-errors-spki-list={sites.spki_hash}")
    async with BrowserPool(launch_args=args) as pool:
        new_context = pool._new_context

        async def configured_context(*args: object) -> object:
            context = await new_context(*args)
            new_page = context.new_page

            async def configured_page() -> Page:
                page = await new_page()
                await set_third_party_cookie_restriction(page, blocked)
                return page

            context.new_page = configured_page
            return context

        monkeypatch.setattr(pool, "_new_context", configured_context)

        async def load_image(page: Page) -> None:
            await page.evaluate(
                """url => new Promise(resolve => {
                    const img = new Image();
                    img.onload = resolve;
                    img.onerror = resolve;
                    img.src = url;
                    document.body.append(img);
                })""",
                f"{sites.ip_origin}/img.svg?tag=guarded-{blocked}",
            )

        auth = RenderAuth(
            cookies=[
                Cookie(
                    name=item["name"],
                    value=item["value"],
                    url=item["url"],
                    secure=True,
                    same_site=item["sameSite"],
                )
                for item in cookies
            ]
        )
        async with AsyncRenderer(pool=pool, allow_private_network=True) as renderer:
            await renderer.from_url(
                f"{sites.loopback_origin}/secret", auth=auth, prepare=load_image
            )

    (baseline,) = sites.received(f"baseline-{blocked}")
    (guarded,) = sites.received(f"guarded-{blocked}")
    baseline_cookie = baseline.headers.get("cookie", "")
    assert guarded.headers.get("cookie", "") == baseline_cookie
    if blocked:
        assert baseline_cookie == ""
    else:
        assert baseline_cookie == "none=value"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("blocked", [False, True], ids=["allowed", "blocked"])
async def test_https_cookie_matrix_matches_chromium_for_every_source(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    blocked: bool,
) -> None:
    sites = https_sites
    (tmp_path / "file.html").write_text("<p>source</p>")
    (tmp_path / "page.html").write_text("{{ note }}")
    cookies = [
        {
            "name": name,
            "value": "value",
            "url": sites.ip_origin,
            "sameSite": same_site,
            "secure": True,
        }
        for name, same_site in (("strict", "Strict"), ("lax", "Lax"), ("none", "None"))
    ]

    async def exercise(page: Page, tag: str) -> None:
        await page.evaluate(
            """async ({origin, tag}) => {
                const image = new Image();
                const loaded = new Promise(resolve => {
                    image.onload = resolve;
                    image.onerror = resolve;
                });
                image.src = `${origin}/img.svg?tag=${tag}-image`;
                document.body.append(image);
                await Promise.all([
                    loaded,
                    fetch(`${origin}/auth/cors-api?tag=${tag}-fetch`,
                          {mode: 'no-cors', credentials: 'include'}).catch(() => null),
                ]);
            }""",
            {"origin": sites.ip_origin, "tag": tag},
        )

    async with test_browser_pool.context() as context:
        await context.add_cookies(cookies)
        if source in {"bundle", "bundle_template"}:
            await context.grant_permissions(["local-network-access"], origin=BUNDLE_ORIGIN)
            await context.route(
                f"{BUNDLE_ORIGIN}/**",
                lambda route: route.fulfill(
                    status=200, body="<p>source</p>", content_type="text/html"
                ),
            )
        page = await context.new_page()
        await set_third_party_cookie_restriction(page, blocked)
        await load_test_source(page, source, sites, tmp_path)
        await exercise(page, f"baseline-{source}-{blocked}")

    auth = RenderAuth(
        cookies=[
            Cookie(
                name=item["name"],
                value=item["value"],
                url=item["url"],
                secure=True,
                same_site=item["sameSite"],
            )
            for item in cookies
        ]
    )
    async with (
        guarded_https_pool(sites, monkeypatch, blocked) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):

        async def prepared(page: Page) -> None:
            await exercise(page, f"guarded-{source}-{blocked}")

        await render_test_source(renderer, source, sites, tmp_path, auth, prepared)

    for shape in ("image", "fetch"):
        baseline_requests = sites.received(f"baseline-{source}-{blocked}-{shape}")
        guarded_requests = sites.received(f"guarded-{source}-{blocked}-{shape}")
        assert len(guarded_requests) == len(baseline_requests)
        if not baseline_requests:
            assert shape == "fetch"
            continue
        (baseline,) = baseline_requests
        (guarded,) = guarded_requests
        baseline_cookie = baseline.headers.get("cookie", "")
        assert guarded.headers.get("cookie", "") == baseline_cookie
        if source == "url":
            assert baseline_cookie == ("" if blocked else "none=value")


@pytest.mark.parametrize("return_to_a", [False, True], ids=["a-b", "a-b-a"])
@pytest.mark.parametrize("blocked", [False, True], ids=["allowed", "blocked"])
async def test_https_redirect_hops_match_chromium_cookies_and_exact_origin_headers(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
    return_to_a: bool,
    blocked: bool,
) -> None:
    sites = https_sites
    cookies = [
        {
            "name": f"{site}-{same_site.lower()}",
            "value": "value",
            "url": origin,
            "sameSite": same_site,
            "secure": True,
        }
        for site, origin in (("a", sites.loopback_origin), ("b", sites.ip_origin))
        for same_site in ("Strict", "Lax", "None")
    ]

    def chain(prefix: str) -> str:
        end = f"{sites.loopback_origin}/img.svg?tag={prefix}-a2"
        target = (
            f"{sites.ip_origin}/redirect?tag={prefix}-b&to={quote(end, safe='')}"
            if return_to_a
            else f"{sites.ip_origin}/img.svg?tag={prefix}-b"
        )
        return f"{sites.loopback_origin}/redirect?tag={prefix}-a0&to={quote(target, safe='')}"

    async def image(page: Page, url: str) -> None:
        await page.evaluate(
            """url => new Promise(resolve => {
                const image = new Image();
                image.onload = resolve;
                image.onerror = resolve;
                image.src = url;
                document.body.append(image);
            })""",
            url,
        )

    async with test_browser_pool.context() as context:
        await context.add_cookies(cookies)
        page = await context.new_page()
        await set_third_party_cookie_restriction(page, blocked)
        await page.goto(f"{sites.loopback_origin}/secret")
        await image(page, chain(f"baseline-{return_to_a}-{blocked}"))

    auth = RenderAuth(
        cookies=[
            Cookie(
                name=item["name"],
                value=item["value"],
                url=item["url"],
                secure=True,
                same_site=item["sameSite"],
            )
            for item in cookies
        ],
        headers={
            sites.loopback_origin: {"X-Site": "a"},
            sites.ip_origin: {"X-Site": "b"},
        },
    )
    async with (
        guarded_https_pool(sites, monkeypatch, blocked) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):

        async def prepared(page: Page) -> None:
            await image(page, chain(f"guarded-{return_to_a}-{blocked}"))

        await renderer.from_url(f"{sites.loopback_origin}/secret", auth=auth, prepare=prepared)

    def exact(tag: str) -> list[object]:
        return [
            record
            for record in sites.records
            if parse_qs(urlsplit(record.path).query).get("tag") == [tag]
        ]

    for hop, site in (("a0", "a"), ("b", "b"), *([("a2", "a")] if return_to_a else [])):
        (baseline,) = exact(f"baseline-{return_to_a}-{blocked}-{hop}")
        (guarded,) = exact(f"guarded-{return_to_a}-{blocked}-{hop}")
        assert guarded.headers.get("cookie", "") == baseline.headers.get("cookie", "")
        assert guarded.headers.get("x-site") == site


@pytest.mark.parametrize("status", [307, 308])
async def test_https_post_redirect_preserves_body_and_browser_cookies(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    sites = https_sites

    def urls(prefix: str) -> tuple[str, str]:
        target = f"{sites.ip_origin}/auth/echo?tag={prefix}-b"
        first = (
            f"{sites.loopback_origin}/redirect-post?status={status}"
            f"&tag={prefix}-a&to={quote(target, safe='')}"
        )
        return first, target

    async def post(page: Page, first: str) -> None:
        await page.evaluate(
            """url => fetch(url, {
                method: 'POST', body: 'payload', mode: 'no-cors', credentials: 'include'
            }).catch(() => null)""",
            first,
        )

    cookie = {
        "name": "post",
        "value": "browser",
        "url": sites.ip_origin,
        "sameSite": "None",
        "secure": True,
    }
    async with test_browser_pool.context() as context:
        await context.add_cookies([cookie])
        page = await context.new_page()
        await set_third_party_cookie_restriction(page, False)
        await page.goto(f"{sites.loopback_origin}/secret")
        await post(page, urls(f"baseline-{status}")[0])

    auth = RenderAuth(
        cookies=[
            Cookie(name="post", value="browser", url=sites.ip_origin, same_site="None", secure=True)
        ],
        headers={sites.loopback_origin: {"X-Site": "a"}, sites.ip_origin: {"X-Site": "b"}},
    )
    async with (
        guarded_https_pool(sites, monkeypatch, False) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):

        async def prepared(page: Page) -> None:
            await post(page, urls(f"guarded-{status}")[0])

        await renderer.from_url(f"{sites.loopback_origin}/secret", auth=auth, prepare=prepared)

    def exact(tag: str) -> list[object]:
        return [
            record
            for record in sites.records
            if parse_qs(urlsplit(record.path).query).get("tag") == [tag]
        ]

    for hop, site in (("a", "a"), ("b", "b")):
        (baseline,) = exact(f"baseline-{status}-{hop}")
        (guarded,) = exact(f"guarded-{status}-{hop}")
        assert baseline.method == guarded.method == "POST"
        assert baseline.body == guarded.body == b"payload"
        assert guarded.headers.get("cookie", "") == baseline.headers.get("cookie", "")
        assert guarded.headers.get("x-site") == site


@pytest.mark.parametrize("blocked", [False, True], ids=["allowed", "blocked"])
async def test_https_redirect_set_cookie_matches_chromium_next_hop(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
    blocked: bool,
) -> None:
    sites = https_sites

    def first(prefix: str) -> str:
        target = f"{sites.ip_origin}/img.svg?tag={prefix}-end"
        return f"{sites.ip_origin}/set-cookie-redirect?to={quote(target, safe='')}"

    async def image(page: Page, url: str) -> None:
        await page.evaluate(
            """url => new Promise(resolve => {
                const image = new Image();
                image.onload = resolve;
                image.onerror = resolve;
                image.src = url;
                document.body.append(image);
            })""",
            url,
        )

    async with test_browser_pool.context() as context:
        page = await context.new_page()
        await set_third_party_cookie_restriction(page, blocked)
        await page.goto(f"{sites.loopback_origin}/secret")
        await image(page, first(f"baseline-{blocked}"))

    async with (
        guarded_https_pool(sites, monkeypatch, blocked) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):

        async def prepared(page: Page) -> None:
            await image(page, first(f"guarded-{blocked}"))

        await renderer.from_url(f"{sites.loopback_origin}/secret", prepare=prepared)

    (baseline,) = sites.received(f"baseline-{blocked}-end")
    (guarded,) = sites.received(f"guarded-{blocked}-end")
    baseline_cookie = baseline.headers.get("cookie", "")
    assert guarded.headers.get("cookie", "") == baseline_cookie
    assert baseline_cookie == ("" if blocked else "new=from-redirect")


async def test_https_redirect_to_same_host_other_port_rebuilds_headers(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sites = https_sites

    def first(prefix: str) -> str:
        target = f"{sites.ip_other_port_origin}/img.svg?tag={prefix}-other"
        return f"{sites.ip_origin}/redirect?tag={prefix}-first&to={quote(target, safe='')}"

    async def image(page: Page, url: str) -> None:
        await page.evaluate(
            """url => new Promise(resolve => {
                const image = new Image();
                image.onload = resolve;
                image.onerror = resolve;
                image.src = url;
                document.body.append(image);
            })""",
            url,
        )

    async with test_browser_pool.context() as context:
        await context.add_cookies(
            [
                {
                    "name": "portless",
                    "value": "browser",
                    "url": sites.ip_origin,
                    "sameSite": "None",
                    "secure": True,
                }
            ]
        )
        page = await context.new_page()
        await page.goto(f"{sites.ip_origin}/secret")
        await image(page, first("baseline-port"))

    auth = RenderAuth(
        cookies=[
            Cookie(
                name="portless", value="browser", url=sites.ip_origin, same_site="None", secure=True
            )
        ],
        headers={
            sites.ip_origin: {"X-Port": "first"},
            sites.ip_other_port_origin: {"X-Port": "other"},
        },
    )
    async with (
        guarded_https_pool(sites, monkeypatch, False) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):

        async def prepared(page: Page) -> None:
            await image(page, first("guarded-port"))

        await renderer.from_url(f"{sites.ip_origin}/secret", auth=auth, prepare=prepared)

    for hop, port_value in (("first", "first"), ("other", "other")):
        (baseline,) = sites.received(f"baseline-port-{hop}")
        (guarded,) = sites.received(f"guarded-port-{hop}")
        assert guarded.headers.get("cookie", "") == baseline.headers.get("cookie", "")
        assert guarded.headers.get("x-port") == port_value
        assert guarded.host == (
            sites.ip_origin if hop == "first" else sites.ip_other_port_origin
        ).removeprefix("https://")


async def test_safe_top_level_get_redirect_matches_chromium_cookies(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sites = https_sites

    def first(prefix: str) -> str:
        target = f"{sites.ip_origin}/secret?tag={prefix}-end"
        return f"{sites.loopback_origin}/redirect?to={quote(target, safe='')}"

    cookies = [
        {
            "name": name,
            "value": "value",
            "url": sites.ip_origin,
            "sameSite": same_site,
            "secure": True,
        }
        for name, same_site in (("strict", "Strict"), ("lax", "Lax"), ("none", "None"))
    ]
    async with test_browser_pool.context() as context:
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto(first("baseline-top"))

    auth = RenderAuth(
        cookies=[
            Cookie(
                name=item["name"],
                value=item["value"],
                url=item["url"],
                same_site=item["sameSite"],
                secure=True,
            )
            for item in cookies
        ]
    )
    async with (
        guarded_https_pool(sites, monkeypatch, False) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):
        await renderer.from_url(first("guarded-top"), auth=auth)

    (baseline,) = sites.received("baseline-top-end")
    (guarded,) = sites.received("guarded-top-end")
    baseline_cookie = baseline.headers.get("cookie", "")
    assert guarded.headers.get("cookie", "") == baseline_cookie
    assert "lax=value" in baseline_cookie


async def test_https_to_http_top_level_redirect_matches_browser_cookie_and_header_scope(
    https_sites: HttpsSites,
    test_browser_pool: BrowserPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sites = https_sites

    def first(prefix: str) -> str:
        target = f"{sites.ip_http_origin}/secret?tag={prefix}-http"
        return f"{sites.ip_origin}/redirect?tag={prefix}-https&to={quote(target, safe='')}"

    cookies = [
        {
            "name": "plain",
            "value": "browser",
            "url": sites.ip_http_origin,
            "sameSite": "Lax",
            "secure": False,
        },
        {
            "name": "secure",
            "value": "browser",
            "url": sites.ip_origin,
            "sameSite": "None",
            "secure": True,
        },
    ]
    async with test_browser_pool.context() as context:
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto(first("baseline-scheme"))

    auth = RenderAuth(
        cookies=[
            Cookie(
                name=item["name"],
                value=item["value"],
                url=item["url"],
                same_site=item["sameSite"],
                secure=item["secure"],
            )
            for item in cookies
        ],
        headers={
            sites.ip_origin: {"X-Scheme": "https"},
            sites.ip_http_origin: {"X-Scheme": "http"},
        },
    )
    async with (
        guarded_https_pool(sites, monkeypatch, False) as pool,
        AsyncRenderer(pool=pool, allow_private_network=True) as renderer,
    ):
        await renderer.from_url(first("guarded-scheme"), auth=auth)

    for hop, scheme in (("https", "https"), ("http", "http")):
        (baseline,) = sites.received_exact(f"baseline-scheme-{hop}")
        (guarded,) = sites.received_exact(f"guarded-scheme-{hop}")
        assert guarded.headers.get("cookie", "") == baseline.headers.get("cookie", "")
        assert guarded.headers.get("x-scheme") == scheme
