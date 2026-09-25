"""Waiting until a page is really ready to print."""

from __future__ import annotations

from playwright.async_api import Page

from dravenpdf.options import RenderOptions

READY_FLAG = "__DRAVENPDF_READY__"

# Load lazy images now instead of when they scroll into view, then wait for every
# image to finish (or fail). Broken images must not block the render.
_LOAD_IMAGES_JS = """
async () => {
  for (const img of document.querySelectorAll('img[loading="lazy"]')) img.loading = 'eager';
  window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
  window.scrollTo(0, 0);
  await Promise.all(Array.from(document.images, img => img.complete ? null :
    new Promise(done => {
      img.addEventListener('load', done);
      img.addEventListener('error', done);
    })));
  return true;
}
"""

_FONTS_READY_JS = "async () => { await document.fonts.ready; return true; }"


async def wait_until_ready(page: Page, options: RenderOptions) -> None:
    """Run all waits the options ask for. Timeouts come from the page's default timeout."""
    if options.wait_for_selector is not None:
        await page.wait_for_selector(options.wait_for_selector, state="attached")
    if options.wait_for_ready_flag:
        await page.wait_for_function(f"() => window.{READY_FLAG} === true")
    await page.evaluate(_LOAD_IMAGES_JS)
    await page.evaluate(_FONTS_READY_JS)
