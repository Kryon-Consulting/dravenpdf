"""Request and response bodies for the JSON endpoints."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dravenpdf.document.stamp import Position
from dravenpdf.options import RenderOptions

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(value: str) -> str:
    """A download name made of safe characters, ending in .pdf."""
    name = _SAFE_FILENAME.sub("_", value).strip("._") or "document"
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StampTextSpec(_Body):
    text: str = Field(min_length=1)
    font_size: float = Field(default=48, gt=0)
    color: str = "#FF0000"
    opacity: float = Field(default=0.3, gt=0, le=1)
    angle: float = 45
    position: Position = "center"
    margin: float = Field(default=36, ge=0)


class MetadataSpec(_Body):
    title: str | None = None
    author: str | None = None
    subject: str | None = None
    keywords: str | None = None


class PostProcess(_Body):
    """Optional work applied to the rendered PDF before it is returned."""

    stamp_text: StampTextSpec | None = None
    metadata: MetadataSpec | None = None
    compress: bool = False


class _RenderRequest(_Body):
    options: RenderOptions = Field(default_factory=RenderOptions)
    post: PostProcess | None = None
    filename: str = Field(default="document.pdf", max_length=200)

    @field_validator("filename")
    @classmethod
    def _safe_filename(cls, value: str) -> str:
        return safe_filename(value)


class RenderHtmlRequest(_RenderRequest):
    html: str = Field(min_length=1)
    base_url: str | None = None


class RenderUrlRequest(_RenderRequest):
    url: str = Field(min_length=1)


class RenderTemplateRequest(_RenderRequest):
    template: str = Field(min_length=1, description="Jinja2 template source.")
    data: dict[str, Any] = Field(default_factory=dict)
    base_url: str | None = None


class TextResponse(BaseModel):
    pages: list[str]
