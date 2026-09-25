from __future__ import annotations

import pytest

from dravenpdf import BlockedRequestError, RequestGuard

DNS = {
    "public.example": ["93.184.216.34"],
    "internal.example": ["10.0.0.5"],
    "mixed.example": ["93.184.216.34", "192.168.1.10"],
    "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    "cgnat.example": ["100.64.0.1"],
}


async def fake_resolver(host: str) -> list[str]:
    if host not in DNS:
        raise OSError("Name or service not known")
    return DNS[host]


def guard(**kwargs: object) -> RequestGuard:
    return RequestGuard(resolver=fake_resolver, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        "https://public.example/page",
        "http://public.example:8080/x?y=1",
        "https://v6.example/",
        "http://93.184.216.34/",
        "data:text/html,<p>x</p>",
        "about:blank",
        "blob:https://public.example/1234",
    ],
)
async def test_default_allows_public(url: str) -> None:
    await guard().check(url)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://internal.example/", "non-public"),
        ("http://mixed.example/", "non-public"),
        ("http://cgnat.example/", "non-public"),
        ("http://127.0.0.1:8000/", "non-public"),
        ("http://10.1.2.3/", "non-public"),
        ("http://169.254.169.254/latest/meta-data/", "non-public"),
        ("http://[::1]/", "non-public"),
        ("http://[::ffff:127.0.0.1]/", "non-public"),
        ("http://0.0.0.0/", "non-public"),
        ("http://224.0.0.1/", "non-public"),
        ("http://nowhere.example/", "could not resolve"),
        ("file:///etc/passwd", "scheme 'file'"),
        ("ftp://public.example/", "scheme 'ftp'"),
        ("javascript:alert(1)", "scheme 'javascript'"),
        ("http:///nohost", "no host"),
    ],
)
async def test_default_blocks(url: str, reason: str) -> None:
    with pytest.raises(BlockedRequestError, match=reason) as info:
        await guard().check(url)
    assert info.value.url == url


async def test_allowlist_exact_and_wildcard() -> None:
    g = guard(allowed_hosts=["Public.Example", "*.cdn.example"])

    await g.check("https://public.example/")
    await g.check("https://img.cdn.example/a.png")
    await g.check("https://a.b.cdn.example/a.png")
    for url in ("https://cdn.example/", "https://other.example/", "https://notpublic.example/"):
        with pytest.raises(BlockedRequestError, match="allowlist"):
            await g.check(url)


async def test_allowlist_lets_named_internal_hosts_through() -> None:
    g = guard(allowed_hosts=["internal.example", "localhost"])

    await g.check("http://internal.example/")
    await g.check("http://localhost:9000/")
    with pytest.raises(BlockedRequestError):
        await g.check("http://127.0.0.1:9000/")  # same machine, but not by that name


async def test_allow_private_network() -> None:
    g = guard(allow_private_network=True)

    await g.check("http://127.0.0.1/")
    await g.check("http://internal.example/")
    with pytest.raises(BlockedRequestError, match="scheme"):
        await g.check("file:///etc/passwd")


def test_checks_requests() -> None:
    assert guard().checks_requests
    assert guard(allowed_hosts=[]).checks_requests
    assert guard(allowed_hosts=["x"], allow_private_network=True).checks_requests
    assert not guard(allow_private_network=True).checks_requests
    assert guard(allow_private_network=True, file_root="/tmp").checks_requests


async def test_empty_allowlist_blocks_everything_remote() -> None:
    with pytest.raises(BlockedRequestError):
        await guard(allowed_hosts=[]).check("https://public.example/")


async def test_dns_is_cached_per_guard() -> None:
    calls: list[str] = []

    async def counting(host: str) -> list[str]:
        calls.append(host)
        return ["93.184.216.34"]

    g = RequestGuard(resolver=counting)
    await g.check("https://public.example/a")
    await g.check("https://public.example/b")

    assert calls == ["public.example"]


def test_raise_if_blocked_reports_first_and_count() -> None:
    g = guard()
    g.raise_if_blocked()  # nothing blocked yet

    g.blocked.extend([("http://10.0.0.1/a", "private"), ("http://10.0.0.1/b", "private")])
    with pytest.raises(BlockedRequestError, match=r"10\.0\.0\.1/a.*1 more") as info:
        g.raise_if_blocked()
    assert info.value.url == "http://10.0.0.1/a"


async def test_file_root(tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path)) / "site"
    (root / "img").mkdir(parents=True)
    g = guard(file_root=root)

    await g.check((root / "index.html").as_uri())
    await g.check((root / "img" / "a b.png").as_uri())
    for url in (
        (root.parent / "secret.txt").as_uri(),
        (root / ".." / "secret.txt").as_uri(),
        "file:///etc/passwd",
        (root.parent / "site-other" / "x").as_uri(),  # shares a prefix, not a folder
    ):
        with pytest.raises(BlockedRequestError, match="outside"):
            await g.check(url)


async def test_file_root_blocks_symlink_escape(tmp_path: object) -> None:
    from pathlib import Path

    base = Path(str(tmp_path))
    (base / "site").mkdir()
    (base / "secret.txt").write_text("x")
    (base / "site" / "link.txt").symlink_to(base / "secret.txt")

    with pytest.raises(BlockedRequestError, match="outside"):
        await guard(file_root=base / "site").check((base / "site" / "link.txt").as_uri())


@pytest.mark.parametrize(
    ("url", "blocked"),
    [
        ("wss://public.example/socket", False),
        ("ws://public.example/socket", False),
        ("ws://127.0.0.1:8080/", True),
        ("wss://internal.example/", True),
        ("ws://[::1]/", True),
    ],
)
async def test_websockets_follow_the_http_rules(url: str, blocked: bool) -> None:
    if blocked:
        with pytest.raises(BlockedRequestError, match="non-public"):
            await guard().check(url)
    else:
        await guard().check(url)


async def test_websocket_allowlist() -> None:
    g = guard(allowed_hosts=["localhost"])

    await g.check("ws://localhost:9000/live")
    with pytest.raises(BlockedRequestError, match="allowlist"):
        await g.check("wss://public.example/")
