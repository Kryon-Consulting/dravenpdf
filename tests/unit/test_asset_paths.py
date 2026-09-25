from __future__ import annotations

import pytest

from dravenpdf import AssetError
from dravenpdf.render.assets import MAX_FILES, ORIGIN, AssetBundle, check_path, content_type


@pytest.mark.parametrize("path", ["a.css", "css/site.css", "img/deep/logo.png", "my file.png"])
def test_valid_paths(path: str) -> None:
    assert check_path(path) == path


@pytest.mark.parametrize(
    "path",
    ["", "/abs.css", "../up.css", "a/../b.css", "a//b.css", "./a.css", "a\\b.css",
     "C:/x.css", "a\x00.css", "x" * 256],
)  # fmt: skip
def test_invalid_paths(path: str) -> None:
    with pytest.raises(AssetError):
        check_path(path)


def test_too_many_files() -> None:
    with pytest.raises(AssetError, match="too many"):
        AssetBundle("", {f"f{i}.txt": b"" for i in range(MAX_FILES + 1)})


@pytest.fixture
def bundle() -> AssetBundle:
    return AssetBundle("<p>doc</p>", {"css/site.css": b"css", "img/my logo.png": b"png"})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (ORIGIN + "/", (b"<p>doc</p>", "text/html; charset=utf-8")),
        (ORIGIN + "/css/site.css", (b"css", "text/css")),
        (ORIGIN + "/css/site.css?v=3#top", (b"css", "text/css")),
        (ORIGIN + "/img/my%20logo.png", (b"png", "image/png")),
        (ORIGIN + "/img/../css/site.css", (b"css", "text/css")),
        (ORIGIN + "/missing.js", None),
        (ORIGIN + "/../../etc/passwd", None),
    ],
)
def test_lookup(bundle: AssetBundle, url: str, expected: tuple[bytes, str] | None) -> None:
    assert bundle.lookup(url) == expected


@pytest.mark.parametrize(
    ("url", "owned"),
    [
        (ORIGIN + "/x", True),
        ("https://BUNDLE.dravenpdf.invalid/x", True),
        ("http://bundle.dravenpdf.invalid/x", False),
        ("wss://bundle.dravenpdf.invalid/x", False),
        ("https://bundle.dravenpdf.invalid.evil.example/x", False),
    ],
)
def test_owns(url: str, owned: bool) -> None:
    assert AssetBundle.owns(url) is owned


@pytest.mark.parametrize(
    ("path", "kind"),
    [("f.woff2", "font/woff2"), ("a.SVG", "image/svg+xml"), ("app.js", "text/javascript"),
     ("x.unknownext", "application/octet-stream")],
)  # fmt: skip
def test_content_type(path: str, kind: str) -> None:
    assert content_type(path) == kind
