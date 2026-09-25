"""Jinja2 templates to HTML.

Templates run in Jinja2's sandbox with HTML autoescaping on, so template data
can't inject markup and templates can't reach into Python internals. Templates
loaded from a folder can include or extend other templates in that folder only.
"""

from __future__ import annotations

from collections.abc import Mapping
from os import PathLike
from typing import Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from dravenpdf.errors import TemplateError


def _environment(template_dir: str | PathLike[str] | None) -> SandboxedEnvironment:
    loader = None if template_dir is None else jinja2.FileSystemLoader(template_dir)
    return SandboxedEnvironment(loader=loader, autoescape=True)


def render_template(
    template: str,
    data: Mapping[str, Any],
    *,
    template_dir: str | PathLike[str] | None = None,
) -> str:
    """Render to HTML.

    With ``template_dir``, ``template`` is a file name inside that folder. Without it,
    ``template`` is the template source itself.
    """
    env = _environment(template_dir)
    try:
        if template_dir is None:
            return env.from_string(template).render(**data)
        return env.get_template(template).render(**data)
    except jinja2.TemplateNotFound as exc:
        raise TemplateError(f"template not found: {exc.name}") from exc
    except jinja2.TemplateSyntaxError as exc:
        raise TemplateError(f"template syntax error on line {exc.lineno}: {exc.message}") from exc
    except jinja2.TemplateError as exc:  # includes sandbox SecurityError, UndefinedError
        raise TemplateError(f"template error: {exc}") from exc
