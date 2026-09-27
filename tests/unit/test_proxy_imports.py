"""The proxy subprocess starts once per render, so its imports must stay light."""

from __future__ import annotations

import os
import subprocess
import sys

HEAVY = {"pikepdf", "pypdfium2", "playwright", "pydantic", "jinja2", "PIL", "img2pdf", "pyhanko"}


def test_proxy_addon_imports_no_heavy_packages() -> None:
    code = (
        "import sys\n"
        "import dravenpdf.render._proxy_addon\n"
        f"print(sorted({{m.split('.')[0] for m in sys.modules}} & set({sorted(HEAVY)!r})))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("DRAVENPDF_PROXY_")}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    )

    assert result.stdout.strip() == "[]"


def test_package_exports_still_resolve() -> None:
    import dravenpdf
    import dravenpdf.render

    for name in dravenpdf.__all__:
        assert getattr(dravenpdf, name) is not None
    for name in dravenpdf.render.__all__:
        assert getattr(dravenpdf.render, name) is not None
