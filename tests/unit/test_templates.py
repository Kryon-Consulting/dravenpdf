from __future__ import annotations

from pathlib import Path

import pytest

from dravenpdf import TemplateError
from dravenpdf.render.templates import render_template


def test_string_template() -> None:
    html = render_template(
        "<p>{{ name }}: {{ items | length }}</p>", {"name": "Ada", "items": [1, 2]}
    )

    assert html == "<p>Ada: 2</p>"


def test_data_is_escaped() -> None:
    html = render_template("<p>{{ v }}</p>", {"v": "<script>alert(1)</script>"})

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_sandbox_blocks_python_internals() -> None:
    with pytest.raises(TemplateError, match="unsafe"):
        render_template("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})


def test_syntax_error_reports_line() -> None:
    with pytest.raises(TemplateError, match="line 2"):
        render_template("ok\n{% if %}", {})


def test_folder_templates_can_include_siblings(tmp_path: Path) -> None:
    (tmp_path / "base.html").write_text("<h1>{% block title %}{% endblock %}</h1>")
    (tmp_path / "invoice.html").write_text(
        '{% extends "base.html" %}{% block title %}Invoice {{ n }}{% endblock %}'
    )

    assert render_template("invoice.html", {"n": 7}, template_dir=tmp_path) == "<h1>Invoice 7</h1>"


@pytest.mark.parametrize("name", ["missing.html", "../secret.html"])
def test_folder_templates_cannot_escape(tmp_path: Path, name: str) -> None:
    (tmp_path / "secret.html").write_text("secret")
    folder = tmp_path / "templates"
    folder.mkdir()

    with pytest.raises(TemplateError, match="not found"):
        render_template(name, {}, template_dir=folder)
