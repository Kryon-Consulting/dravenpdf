"""Unguarded Chromium's on-wire cookie decisions on local HTTPS sites."""

from __future__ import annotations

import pytest
from playwright.async_api import Page

from dravenpdf.render.pool import BrowserPool
from https_fixture import HttpsSites

pytestmark = pytest.mark.browser


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
