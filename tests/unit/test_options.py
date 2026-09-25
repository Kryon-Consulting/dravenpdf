from __future__ import annotations

import pytest
from pydantic import ValidationError

from dravenpdf import HeaderFooter, Margins, RenderOptions, Viewport


def test_defaults_map_to_a4_portrait() -> None:
    kwargs = RenderOptions().to_pdf_kwargs()

    assert kwargs["format"] == "A4"
    assert kwargs["landscape"] is False
    assert kwargs["print_background"] is True
    assert kwargs["margin"] == {"top": "20mm", "right": "15mm", "bottom": "20mm", "left": "15mm"}
    assert "display_header_footer" not in kwargs
    assert "width" not in kwargs


def test_custom_size_replaces_paper_format() -> None:
    kwargs = RenderOptions(width="100mm", height="150mm").to_pdf_kwargs()

    assert kwargs["width"] == "100mm"
    assert kwargs["height"] == "150mm"
    assert "format" not in kwargs


@pytest.mark.parametrize(("width", "height"), [("100mm", None), (None, "100mm")])
def test_width_and_height_must_come_together(width: str | None, height: str | None) -> None:
    with pytest.raises(ValidationError, match="both width and height"):
        RenderOptions(width=width, height=height)


def test_footer_only_gets_blank_header() -> None:
    footer = '<span class="pageNumber"></span>'
    kwargs = RenderOptions(footer=HeaderFooter(html=footer)).to_pdf_kwargs()

    assert kwargs["display_header_footer"] is True
    assert kwargs["footer_template"] == footer
    assert kwargs["header_template"] == "<span></span>"


@pytest.mark.parametrize("value", ["20mm", "1in", "2.5cm", "96px", "96", " 10mm "])
def test_valid_css_lengths(value: str) -> None:
    assert Margins(top=value).top == value.strip()


@pytest.mark.parametrize("value", ["", "10 mm", "-5mm", "10em", "1e3", "abc"])
def test_invalid_css_lengths(value: str) -> None:
    with pytest.raises(ValidationError, match="invalid CSS length"):
        Margins(top=value)


@pytest.mark.parametrize(
    ("value", "normalized"),
    [("1", "1"), ("1-3", "1-3"), ("1-3, 5", "1-3,5"), (" 2 - 4 ,7 ", "2-4,7")],
)
def test_valid_page_ranges(value: str, normalized: str) -> None:
    opts = RenderOptions(page_ranges=value)

    assert opts.page_ranges == normalized
    assert opts.to_pdf_kwargs()["page_ranges"] == normalized


@pytest.mark.parametrize("value", ["", "0", "3-1", "1-", "a", "1,,2", "0-2"])
def test_invalid_page_ranges(value: str) -> None:
    with pytest.raises(ValidationError, match="page range"):
        RenderOptions(page_ranges=value)


@pytest.mark.parametrize("scale", [0.05, 2.5])
def test_scale_bounds(scale: float) -> None:
    with pytest.raises(ValidationError):
        RenderOptions(scale=scale)


@pytest.mark.parametrize("timeout_ms", [0, 300_001])
def test_timeout_bounds(timeout_ms: int) -> None:
    with pytest.raises(ValidationError):
        RenderOptions(timeout_ms=timeout_ms)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RenderOptions.model_validate({"paper": "A4", "papper": "A3"})


def test_options_are_immutable() -> None:
    opts = RenderOptions()
    with pytest.raises(ValidationError):
        opts.landscape = True  # type: ignore[misc]


def test_round_trips_through_json() -> None:
    opts = RenderOptions(
        paper="Letter",
        landscape=True,
        margins=Margins(top="1in"),
        footer=HeaderFooter(html="<b>x</b>"),
        page_ranges="1-2",
    )

    assert RenderOptions.model_validate_json(opts.model_dump_json()) == opts


# ---------------------------------------------------------------- browser environment


def test_context_kwargs_default_to_chromium_defaults() -> None:
    assert RenderOptions().to_context_kwargs() == {}


def test_context_kwargs() -> None:
    opts = RenderOptions(
        viewport=Viewport(width=1920, height=1080),
        device_scale_factor=2,
        locale="de-DE",
        timezone="Europe/Berlin",
        color_scheme="dark",
        reduced_motion="reduce",
    )

    assert opts.to_context_kwargs() == {
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 2,
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "color_scheme": "dark",
        "reduced_motion": "reduce",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("locale", "german", "invalid locale"),
        ("locale", "de_DE", "invalid locale"),
        ("timezone", "Mars/Olympus", "unknown time zone"),
        ("timezone", "../../etc/passwd", "unknown time zone"),
        ("device_scale_factor", 8, "less than or equal to 4"),
        ("color_scheme", "sepia", "color_scheme"),
    ],
)
def test_environment_validation(field: str, value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        RenderOptions.model_validate({field: value})


@pytest.mark.parametrize(("width", "height"), [(50, 720), (1280, 20_000)])
def test_viewport_bounds(width: int, height: int) -> None:
    with pytest.raises(ValidationError):
        Viewport(width=width, height=height)
