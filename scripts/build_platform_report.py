"""Capture platform views and render a self-contained HTML report through dravenpdf.

Run from the repository root with ``.venv/bin/python scripts/build_platform_report.py``.
The PDF service key comes from DRAVENPDF_API_KEY or the local .env file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright


class _AssetReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "img" and values.get("src"):
            self.paths.add(values["src"])
        elif tag == "link" and values.get("href") and values.get("rel") == "stylesheet":
            self.paths.add(values["href"])


def collect_assets(html: str, root: Path) -> dict[str, bytes]:
    """Read local images and stylesheets referenced by HTML for the bundle endpoint."""
    parser = _AssetReferences()
    parser.feed(html)
    base = root.resolve()
    assets: dict[str, bytes] = {}
    for reference in sorted(parser.paths):
        if reference.startswith("data:"):
            continue
        url = urlsplit(reference)
        if url.scheme or url.netloc or reference.startswith("/"):
            raise ValueError(f"asset must be a local relative path: {reference}")
        relative = Path(url.path)
        if ".." in relative.parts or not relative.parts:
            raise ValueError(f"asset path is outside the report root: {reference}")
        asset = (base / relative).resolve()
        if not asset.is_relative_to(base):
            raise ValueError(f"asset path is outside the report root: {reference}")
        if not asset.is_file():
            raise ValueError(f"missing asset: {reference}")
        assets[relative.as_posix()] = asset.read_bytes()
    return assets


def render_report(
    client: httpx.Client, api_url: str, api_key: str, html: str, assets: dict[str, bytes]
) -> bytes:
    """Send HTML and every referenced asset as one network-independent bundle."""
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("files", ("index.html", html.encode("utf-8"), "text/html"))
    ]
    files.extend(
        (
            "files",
            (path, payload, mimetypes.guess_type(path)[0] or "application/octet-stream"),
        )
        for path, payload in assets.items()
    )
    response = client.post(
        api_url.rstrip("/") + "/v1/render/bundle",
        headers={"X-API-Key": api_key},
        data={
            "options": json.dumps(
                {
                    "paper": "A4",
                    "print_background": True,
                    "tagged": True,
                    "outline": True,
                    "fail_on_resource_errors": True,
                    "fail_on_page_errors": True,
                }
            ),
            "filename": "platform-report.pdf",
        },
        files=files,
    )
    response.raise_for_status()
    if int(response.headers.get("X-DravenPdf-Resource-Errors", "0")):
        raise ValueError("PDF service reported a resource error")
    if response.headers.get("Content-Type", "").split(";", 1)[0] != "application/pdf":
        raise ValueError("PDF service did not return application/pdf")
    if not response.content.startswith(b"%PDF-"):
        raise ValueError("PDF service returned invalid PDF bytes")
    return response.content


def _api_key(env_file: Path) -> str:
    key = os.environ.get("DRAVENPDF_API_KEY")
    if key:
        return key
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DRAVENPDF_API_KEY="):
                return line.partition("=")[2].strip().strip("\"'")
    raise ValueError("set DRAVENPDF_API_KEY or provide an .env file containing it")


async def capture_views(
    platform_url: str,
    manifest: Path,
    images_dir: Path,
    *,
    storage_state: Path | None,
    demo_login: bool,
) -> None:
    """Capture named platform routes using a fresh browser context."""
    origin = urlsplit(platform_url)
    if origin.scheme not in {"http", "https"} or not origin.netloc:
        raise ValueError("platform URL must be an HTTP(S) origin")
    entries = json.loads(await asyncio.to_thread(manifest.read_text, encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("screenshot manifest must be a nonempty list")
    await asyncio.to_thread(images_dir.mkdir, parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,
                reduced_motion="reduce",
                ignore_https_errors=origin.hostname in {"localhost", "127.0.0.1"},
                storage_state=str(storage_state) if storage_state else None,
            )
            page = await context.new_page()
            if demo_login:
                await page.goto(platform_url.rstrip("/") + "/auth/login")
                await page.get_by_role("button", name="Sign in").click()
                try:
                    await page.get_by_role("heading", name="Dashboard", exact=True).wait_for(
                        timeout=20_000
                    )
                except Exception as exc:
                    raise ValueError(
                        "demo login did not reach Dashboard; use --storage-state for this account"
                    ) from exc
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("each screenshot entry must be an object")
                name, route, heading = entry["name"], entry["path"], entry["heading"]
                if not isinstance(name, str) or not name.replace("-", "").isalnum():
                    raise ValueError("screenshot names must use letters, numbers or hyphens")
                if (
                    not isinstance(route, str)
                    or not route.startswith("/")
                    or route.startswith("//")
                ):
                    raise ValueError("screenshot paths must start with one slash")
                await page.goto(platform_url.rstrip("/") + route, wait_until="domcontentloaded")
                await page.get_by_role("heading", name=heading, exact=True).wait_for(timeout=20_000)
                wait_for = entry.get("wait_for")
                if wait_for:
                    await page.get_by_text(wait_for, exact=False).first.wait_for(timeout=20_000)
                wait_until_hidden = entry.get("wait_until_hidden")
                if wait_until_hidden:
                    await page.get_by_text(wait_until_hidden, exact=False).first.wait_for(
                        state="hidden", timeout=20_000
                    )
                await page.screenshot(
                    path=str(images_dir / f"{name}.png"),
                    full_page=False,
                    animations="disabled",
                    clip=entry.get("clip"),
                )
                print(f"captured {name}: {page.url}")
            await context.close()
        finally:
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path, help="Report HTML source")
    parser.add_argument("--output", type=Path, default=Path("output/pdf/platform-report.pdf"))
    parser.add_argument("--asset-root", type=Path, default=Path("."))
    parser.add_argument("--images-dir", type=Path, default=Path("images"))
    parser.add_argument(
        "--capture-manifest", type=Path, help="JSON list of platform views to capture"
    )
    parser.add_argument("--platform-url", default="https://localhost:9000")
    parser.add_argument("--storage-state", type=Path, help="Playwright authentication state JSON")
    parser.add_argument("--demo-login", action="store_true", help="Use the local demo login form")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    if args.capture_manifest:
        asyncio.run(
            capture_views(
                args.platform_url,
                args.capture_manifest,
                args.images_dir,
                storage_state=args.storage_state,
                demo_login=args.demo_login,
            )
        )
    html = args.html.read_text(encoding="utf-8")
    assets = collect_assets(html, args.asset_root)
    with httpx.Client(timeout=90) as client:
        pdf = render_report(client, args.api_url, _api_key(args.env_file), html, assets)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(pdf)
    print(f"wrote {args.output} ({len(pdf):,} bytes)")


if __name__ == "__main__":
    main()
