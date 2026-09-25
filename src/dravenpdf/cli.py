"""The ``dravenpdf`` command line.

A thin layer over the library: parse arguments, call ``Renderer`` / ``PdfDocument``,
write files. Page arguments are 1-based range strings ("1,3-5,8-"), as people
write them. ``-o -`` writes a single PDF to stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from dravenpdf import __version__
from dravenpdf.document.images import ImageFormat
from dravenpdf.document.pages import parse_page_ranges
from dravenpdf.document.pdf import PdfDocument
from dravenpdf.document.stamp import Position
from dravenpdf.errors import BlockedRequestError, DravenPdfError
from dravenpdf.options import HeaderFooter, Margins, PaperSize, RenderOptions, WaitUntil
from dravenpdf.render.sync import Renderer

app = typer.Typer(
    name="dravenpdf",
    help="HTML to PDF conversion and PDF tools.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

# Typer reads these annotations at runtime, so they use ... | None rather than X | None
# inside Annotated where Typer needs the plain type.
Output = Annotated[Path, typer.Option("--output", "-o", help="Output file, or - for stdout.")]
OutDir = Annotated[Path, typer.Option("--output", "-o", help="Output folder.")]
InputPdf = Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Input PDF.")]
PagesOpt = Annotated[
    str | None, typer.Option("--pages", help="1-based pages, e.g. 1,3-5,8- (default: all).")
]


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _pages(spec: str | None, doc: PdfDocument) -> list[int] | None:
    return None if spec is None else parse_page_ranges(spec, doc.page_count)


def _write_pdf(doc: PdfDocument, output: Path, *, compress: bool = False) -> None:
    report = doc.render_report
    if report is not None and not report.ok:
        typer.secho(f"warning: {report.summary()}", fg=typer.colors.YELLOW, err=True)
    data = doc.to_bytes(compress=compress)
    if str(output) == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        output.write_bytes(data)
        typer.echo(f"wrote {output} ({doc.page_count} page{'s' * (doc.page_count != 1)})", err=True)


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def main() -> None:
    """Entry point: run the app, turning library errors into one-line messages."""
    try:
        app()
    except DravenPdfError as exc:
        typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
        if isinstance(exc, BlockedRequestError) and "non-public" in exc.message:
            typer.echo("hint: pass --allow-private to render local or internal addresses", err=True)
        sys.exit(1)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"dravenpdf {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    pass


# ---------------------------------------------------------------- rendering


def _render_options(
    paper: PaperSize,
    landscape: bool,
    margin: str | None,
    header: Path | None,
    footer: Path | None,
    wait_until: WaitUntil,
    wait_for: str | None,
    wait_for_ready_flag: bool,
    timeout: int,
    media: str,
    **extra: Any,
) -> RenderOptions:
    fields: dict[str, Any] = {
        "paper": paper,
        "landscape": landscape,
        "wait_until": wait_until,
        "wait_for_selector": wait_for,
        "wait_for_ready_flag": wait_for_ready_flag,
        "timeout_ms": timeout * 1000,
        "media": media,
        **extra,
    }
    if margin is not None:
        fields["margins"] = Margins(top=margin, right=margin, bottom=margin, left=margin)
    if header is not None:
        fields["header"] = HeaderFooter(html=header.read_text())
    if footer is not None:
        fields["footer"] = HeaderFooter(html=footer.read_text())
    return RenderOptions(**fields)


@app.command()
def render(
    source: Annotated[str, typer.Argument(help="HTML file or http(s) URL.")],
    output: Output,
    paper: Annotated[PaperSize, typer.Option(help="Paper size.")] = "A4",
    landscape: Annotated[bool, typer.Option(help="Landscape orientation.")] = False,
    margin: Annotated[
        str | None, typer.Option(help="All four margins, e.g. 10mm (default 20mm/15mm).")
    ] = None,
    header: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Header HTML file.")
    ] = None,
    footer: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Footer HTML file.")
    ] = None,
    wait_until: Annotated[WaitUntil, typer.Option(help="Page load event to wait for.")] = (
        "networkidle"
    ),
    wait_for: Annotated[
        str | None, typer.Option(help="CSS selector to wait for before printing.")
    ] = None,
    wait_for_ready_flag: Annotated[
        bool, typer.Option(help="Wait for window.__DRAVENPDF_READY__ === true.")
    ] = False,
    media: Annotated[str, typer.Option(help="CSS media: print or screen.")] = "print",
    timeout: Annotated[int, typer.Option(help="Seconds before giving up.")] = 30,
    allow_host: Annotated[
        list[str] | None,
        typer.Option(help="Only load from these hosts (repeatable; *.example.com allowed)."),
    ] = None,
    allow_private: Annotated[
        bool, typer.Option(help="Allow private/localhost addresses, e.g. a local dev server.")
    ] = False,
    skip_blocked: Annotated[
        bool, typer.Option(help="Render without blocked resources instead of failing.")
    ] = False,
    compress: Annotated[bool, typer.Option(help="Compress the output.")] = False,
    fail_on_resource_errors: Annotated[
        bool, typer.Option(help="Fail if an image, stylesheet, font or fetch fails to load.")
    ] = False,
    fail_on_page_errors: Annotated[
        bool, typer.Option(help="Fail if the page throws a JavaScript error.")
    ] = False,
) -> None:
    """Render an HTML file or a web page to PDF. Load problems are printed as warnings."""
    if media not in ("print", "screen"):
        _fail("--media must be print or screen")
    options = _render_options(
        paper, landscape, margin, header, footer, wait_until, wait_for,
        wait_for_ready_flag, timeout, media,
        fail_on_resource_errors=fail_on_resource_errors,
        fail_on_page_errors=fail_on_page_errors,
    )  # fmt: skip
    with Renderer(
        allowed_hosts=allow_host,
        allow_private_network=allow_private,
        on_blocked="skip" if skip_blocked else "fail",
    ) as renderer:
        if _is_url(source):
            doc = renderer.from_url(source, options)
        else:
            path = Path(source)
            if not path.is_file():
                _fail(f"no such file: {source}")
            doc = renderer.from_file(path, options)
    _write_pdf(doc, output, compress=compress)


@app.command()
def template(
    template_file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Jinja2 template file.")
    ],
    output: Output,
    data: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="JSON file with template data."),
    ] = None,
    paper: Annotated[PaperSize, typer.Option(help="Paper size.")] = "A4",
    landscape: Annotated[bool, typer.Option(help="Landscape orientation.")] = False,
    footer: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Footer HTML file.")
    ] = None,
    allow_private: Annotated[bool, typer.Option(help="Allow private/localhost addresses.")] = False,
) -> None:
    """Render a Jinja2 template with JSON data. Assets load from the template's folder."""
    values = json.loads(data.read_text()) if data is not None else {}
    if not isinstance(values, dict):
        _fail("--data must contain a JSON object")
    options = _render_options(
        paper, landscape, None, None, footer, "networkidle", None, False, 30, "print"
    )
    with Renderer(allow_private_network=allow_private) as renderer:
        doc = renderer.from_template(
            template_file.name, values, options, template_dir=template_file.parent
        )
    _write_pdf(doc, output)


# ---------------------------------------------------------------- page operations


@app.command()
def merge(
    inputs: Annotated[list[Path], typer.Argument(exists=True, dir_okay=False, help="PDFs.")],
    output: Output,
) -> None:
    """Merge PDFs in the order given."""
    _write_pdf(PdfDocument.merge([PdfDocument.open(p) for p in inputs]), output)


@app.command()
def split(
    input: InputPdf,
    output: OutDir,
    every: Annotated[int | None, typer.Option(help="Pages per part.")] = None,
    ranges: Annotated[
        list[str] | None, typer.Option("--range", help="One part per range (repeatable).")
    ] = None,
) -> None:
    """Split a PDF into parts: part-1.pdf, part-2.pdf, ..."""
    doc = PdfDocument.open(input)
    parts = doc.iter_split(every=every, ranges=ranges)  # validates before anything is written
    output.mkdir(parents=True, exist_ok=True)
    count = 0
    for part in parts:  # not enumerate(): it would keep the previous part alive
        count += 1
        part.save(output / f"part-{count}.pdf")
        del part
    typer.echo(f"wrote {count} parts to {output}", err=True)


@app.command()
def extract(
    input: InputPdf,
    pages: Annotated[str, typer.Argument(help="1-based pages, e.g. 2-5,8.")],
    output: Output,
) -> None:
    """Copy some pages into a new PDF."""
    _write_pdf(PdfDocument.open(input).extract(pages), output)


@app.command()
def rotate(
    input: InputPdf,
    output: Output,
    degrees: Annotated[int, typer.Option(help="Clockwise, a multiple of 90.")] = 90,
    pages: PagesOpt = None,
) -> None:
    """Rotate pages."""
    doc = PdfDocument.open(input)
    _write_pdf(doc.rotate(degrees, _pages(pages, doc)), output)


@app.command()
def delete(
    input: InputPdf,
    pages: Annotated[str, typer.Argument(help="1-based pages to remove, e.g. 1,3.")],
    output: Output,
) -> None:
    """Remove pages."""
    doc = PdfDocument.open(input)
    _write_pdf(doc.delete(parse_page_ranges(pages, doc.page_count)), output)


@app.command()
def stamp(
    input: InputPdf,
    output: Output,
    text: Annotated[str | None, typer.Option(help="Text watermark (cp1252).")] = None,
    image: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Image to stamp.")
    ] = None,
    html: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="HTML file to stamp.")
    ] = None,
    pdf: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="PDF whose first page to overlay."),
    ] = None,
    opacity: Annotated[float | None, typer.Option(help="0-1.")] = None,
    angle: Annotated[float, typer.Option(help="Text angle, degrees.")] = 45,
    font_size: Annotated[float, typer.Option(help="Text size, points.")] = 48,
    color: Annotated[str, typer.Option(help="Text color, #RRGGBB.")] = "#FF0000",
    width: Annotated[float | None, typer.Option(help="Image width, points.")] = None,
    position: Annotated[Position, typer.Option(help="Where to put it.")] = "center",
    under: Annotated[bool, typer.Option(help="Put it behind the page content.")] = False,
    pages: PagesOpt = None,
) -> None:
    """Stamp text, an image, HTML or another PDF's page onto pages."""
    chosen = [x for x in (text, image, html, pdf) if x is not None]
    if len(chosen) != 1:
        _fail("give exactly one of --text, --image, --html or --pdf")
    doc = PdfDocument.open(input)
    targets = _pages(pages, doc)
    if text is not None:
        result = doc.stamp_text(
            text, font_size=font_size, color=color, opacity=0.3 if opacity is None else opacity,
            angle=angle, position=position, pages=targets, under=under,
        )  # fmt: skip
    elif image is not None:
        result = doc.stamp_image(
            image.read_bytes(), width=width, position=position,
            opacity=1.0 if opacity is None else opacity, pages=targets, under=under,
        )  # fmt: skip
    elif pdf is not None:
        result = doc.overlay(
            PdfDocument.open(pdf), opacity=1.0 if opacity is None else opacity,
            pages=targets, under=under,
        )  # fmt: skip
    else:
        assert html is not None
        markup = html.read_text()
        with Renderer() as renderer:
            result = renderer.stamp_html(
                doc, markup, opacity=1.0 if opacity is None else opacity,
                pages=targets, under=under,
            )  # fmt: skip
    _write_pdf(result, output)


@app.command()
def metadata(
    input: InputPdf,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write a copy with changes.")
    ] = None,
    title: Annotated[str | None, typer.Option(help='Set ("" removes).')] = None,
    author: Annotated[str | None, typer.Option(help='Set ("" removes).')] = None,
    subject: Annotated[str | None, typer.Option(help='Set ("" removes).')] = None,
    keywords: Annotated[str | None, typer.Option(help='Set ("" removes).')] = None,
) -> None:
    """Show document info, or write a copy with fields changed."""
    doc = PdfDocument.open(input)
    changes = {"title": title, "author": author, "subject": subject, "keywords": keywords}
    if all(v is None for v in changes.values()):
        info = {"pages": doc.page_count, **doc.metadata}
        typer.echo(json.dumps(info, indent=2, ensure_ascii=False))
        return
    if output is None:
        _fail("give -o to write the changed copy")
    assert output is not None
    _write_pdf(doc.set_metadata(**changes), output)


@app.command()
def compress(input: InputPdf, output: Output) -> None:
    """Rewrite a PDF smaller (object streams, recompression, unused resources)."""
    before = input.stat().st_size
    doc = PdfDocument.open(input)
    _write_pdf(doc, output, compress=True)
    if str(output) != "-":
        typer.echo(f"{before} -> {output.stat().st_size} bytes", err=True)


# ---------------------------------------------------------------- images and text


@app.command()
def images(
    input: InputPdf,
    output: OutDir,
    dpi: Annotated[int, typer.Option(help="Resolution.")] = 150,
    fmt: Annotated[ImageFormat, typer.Option("--format", help="png or jpeg.")] = "png",
    pages: PagesOpt = None,
) -> None:
    """Render pages to images: page-1.png, page-2.png, ..."""
    doc = PdfDocument.open(input)
    targets = _pages(pages, doc) or list(range(doc.page_count))
    output.mkdir(parents=True, exist_ok=True)
    extension = "jpg" if fmt == "jpeg" else "png"
    for index, data in zip(targets, doc.to_images(dpi=dpi, fmt=fmt, pages=targets), strict=True):
        (output / f"page-{index + 1}.{extension}").write_bytes(data)
    typer.echo(f"wrote {len(targets)} images to {output}", err=True)


@app.command("from-images")
def from_images(
    inputs: Annotated[list[Path], typer.Argument(exists=True, dir_okay=False, help="Images.")],
    output: Output,
    paper: Annotated[
        PaperSize | None, typer.Option(help="Fit onto this paper (default: image size).")
    ] = None,
    landscape: Annotated[bool, typer.Option(help="Landscape paper.")] = False,
    margin: Annotated[float, typer.Option(help="Margin in points, with --paper.")] = 0,
) -> None:
    """One PDF page per image."""
    doc = PdfDocument.from_images(
        [p.read_bytes() for p in inputs], paper=paper, landscape=landscape, margin=margin
    )
    _write_pdf(doc, output)


@app.command()
def text(input: InputPdf) -> None:
    """Print the text of each page (pages separated by form feeds)."""
    typer.echo("\f".join(PdfDocument.open(input).extract_text()))


# ---------------------------------------------------------------- server


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
    workers: Annotated[int, typer.Option(help="Worker processes (one Chromium each).")] = 1,
) -> None:
    """Run the HTTP service (needs the \\[server] extra and DRAVENPDF_API_KEY)."""
    try:
        import uvicorn
    except ImportError:
        _fail('the HTTP service needs the server extra: pip install "dravenpdf[server]"')
    uvicorn.run(
        "dravenpdf.server.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        proxy_headers=True,
    )
