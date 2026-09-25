"""Credentials for rendering pages behind a login.

:class:`RenderAuth` holds everything a render needs to be logged in: cookies,
Playwright storage state (cookies plus ``localStorage`` and IndexedDB per origin),
and extra request headers for exact origins. It is deliberately separate from
:class:`~dravenpdf.options.RenderOptions`, which describes layout.

Every credential value is a :class:`pydantic.SecretStr`, so it is masked in
``repr()``, ``str()``, logs and JSON dumps. Code that must hand a value to the
browser calls ``get_secret_value()`` at that point and nowhere else. Validation
errors don't echo input in their text; note that ``ValidationError.errors()`` still
returns the raw input unless called with ``include_input=False``.

Where each part is applied:

- cookies and storage state go into the render's own, fresh browser context before
  the first navigation, so the browser sends them under its normal cookie rules
  (domain, path, Secure, SameSite) and a later render starts logged out;
- headers are added by the request guard, per request and per redirect hop, only
  when that hop's origin (scheme, host and port) exactly matches a configured one.

Every render source takes the same credentials, and each takes effect by origin, as
in a browser (decision D11):

- headers reach requests to their origin from any page;
- cookies go where Chromium sends them. Requests from a page on another site, which
  includes ``from_html`` (``about:blank``), local files and asset bundles, only carry
  cookies marked ``SameSite=None; Secure``;
- localStorage and IndexedDB belong to their origin: a page on that origin (the page
  itself for ``from_url``, or one it navigates to) sees them. A page on another origin
  doesn't, and Chromium partitions the storage of frames embedded in another site.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Secret,
    SecretStr,
    field_validator,
    model_validator,
)

from dravenpdf.render.assets import ORIGIN as BUNDLE_ORIGIN

MAX_COOKIES = 200
MAX_ORIGINS = 50
MAX_HEADERS_PER_ORIGIN = 30
MAX_LOCAL_STORAGE_ITEMS = 1000
MAX_HEADER_VALUE = 8 * 1024
MAX_COOKIE_VALUE = 4 * 1024
MAX_TOTAL_BYTES = 1024 * 1024  # all secret values together

_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")  # RFC 9110 header / cookie name
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Headers the guard must not set: they describe the connection or body, or would
# bypass the browser's cookie rules (use cookies / storage_state instead).
FORBIDDEN_HEADERS = frozenset(
    {
        "host", "content-length", "transfer-encoding", "connection", "keep-alive",
        "upgrade", "te", "trailer", "expect", "proxy-connection", "cookie", "cookie2",
    }
)  # fmt: skip


def canonical_origin(value: str) -> str:
    """``scheme://host[:port]`` with a lowercase host and the default port dropped.

    Raises ValueError for anything that isn't a bare http(s) origin.
    """
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError("origin must start with http:// or https://")
    if parts.username or parts.password:
        raise ValueError("origin must not contain a user name or password")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("origin must be scheme://host[:port], without a path or query")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("origin has no host")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("origin has an invalid port") from None
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def origin_of(url: str) -> str | None:
    """The canonical origin of an http(s) URL, or None for other URLs."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        return canonical_origin(f"{scheme}://{parts.netloc.rsplit('@', 1)[-1]}")
    except ValueError:
        return None


def _check_secret(value: SecretStr, *, limit: int, what: str) -> SecretStr:
    # Messages here must never include the value itself.
    raw = value.get_secret_value()
    if len(raw) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    if _CONTROL.search(raw):
        raise ValueError(f"{what} contains control characters (e.g. a line break)")
    return value


class _Model(BaseModel):
    # hide_input_in_errors: a ValidationError must never echo a secret value.
    model_config = ConfigDict(
        frozen=True, extra="forbid", populate_by_name=True, hide_input_in_errors=True
    )


class Cookie(_Model):
    """One cookie. Give either ``url``, or ``domain`` (and optionally ``path``)."""

    name: str = Field(min_length=1, max_length=256)
    value: SecretStr
    url: str | None = None
    domain: str | None = Field(default=None, max_length=255)
    path: str | None = Field(default=None, max_length=1024)
    expires: float | None = None
    """Unix time in seconds; None or -1 for a session cookie."""
    http_only: bool = Field(default=False, alias="httpOnly")
    secure: bool = False
    same_site: Literal["Strict", "Lax", "None"] | None = Field(default=None, alias="sameSite")

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _TOKEN.match(value):
            raise ValueError("cookie name has characters that aren't allowed")
        return value

    @field_validator("value")
    @classmethod
    def _valid_value(cls, value: SecretStr) -> SecretStr:
        return _check_secret(value, limit=MAX_COOKIE_VALUE, what="cookie value")

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str | None) -> str | None:
        if value is not None and origin_of(value) is None:
            raise ValueError("cookie url must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _url_or_domain(self) -> Cookie:
        if (self.url is None) == (self.domain is None):
            raise ValueError("give a cookie either url or domain, not both")
        if self.url is not None and self.path is not None:
            raise ValueError("a cookie with url takes its path from the url")
        return self

    def to_playwright(self) -> dict[str, Any]:
        cookie: dict[str, Any] = {"name": self.name, "value": self.value.get_secret_value()}
        if self.url is not None:
            cookie["url"] = self.url
        else:
            cookie["domain"] = self.domain
            cookie["path"] = self.path or "/"
        if self.expires is not None:
            cookie["expires"] = self.expires
        cookie["httpOnly"] = self.http_only
        cookie["secure"] = self.secure
        if self.same_site is not None:
            cookie["sameSite"] = self.same_site
        return cookie


class LocalStorageItem(_Model):
    name: str = Field(min_length=1, max_length=1024)
    value: SecretStr

    @field_validator("value")
    @classmethod
    def _valid_value(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) > MAX_TOTAL_BYTES:
            raise ValueError("localStorage value is too long")
        return value


IndexedDbSnapshot = Secret[list[dict[str, Any]]]
"""Playwright's IndexedDB snapshot for one origin (``storage_state(indexed_db=True)``).
Kept whole and opaque: databases, stores, records and indexes go to Playwright as
they came."""


def _json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


class OriginStorage(_Model):
    origin: str
    local_storage: list[LocalStorageItem] = Field(
        default_factory=list, alias="localStorage", max_length=MAX_LOCAL_STORAGE_ITEMS
    )
    indexed_db: IndexedDbSnapshot | None = Field(default=None, alias="indexedDB")

    @field_validator("origin")
    @classmethod
    def _valid_origin(cls, value: str) -> str:
        return canonical_origin(value)

    @field_validator("indexed_db")
    @classmethod
    def _valid_indexed_db(cls, value: IndexedDbSnapshot | None) -> IndexedDbSnapshot | None:
        if value is None:
            return None
        databases = value.get_secret_value()
        for database in databases:
            if not isinstance(database.get("name"), str) or not isinstance(
                database.get("stores"), list
            ):
                raise ValueError(
                    "indexedDB must be Playwright's snapshot: databases with name and stores"
                )
        if _json_size(databases) > MAX_TOTAL_BYTES:
            raise ValueError(f"indexedDB is larger than {MAX_TOTAL_BYTES} bytes")
        return value

    def secret_size(self) -> int:
        size = sum(len(item.value.get_secret_value()) for item in self.local_storage)
        if self.indexed_db is not None:
            size += _json_size(self.indexed_db.get_secret_value())
        return size


class StorageState(_Model):
    """Playwright's storage-state format (``context.storage_state()`` output), as data.

    Accepts Playwright's own JSON (``httpOnly``, ``sameSite``, ``localStorage``,
    ``indexedDB`` from ``storage_state(indexed_db=True)``) as well as snake_case names.
    """

    cookies: list[Cookie] = Field(default_factory=list, max_length=MAX_COOKIES)
    origins: list[OriginStorage] = Field(default_factory=list, max_length=MAX_ORIGINS)

    @field_validator("cookies", mode="before")
    @classmethod
    def _drop_playwright_extras(cls, value: Any) -> Any:
        # Playwright writes domain *and* path (and sometimes partitionKey) for every
        # cookie; keep what the Cookie model understands.
        if isinstance(value, list):
            keep = {"name", "value", "url", "domain", "path", "expires", "httpOnly",
                    "http_only", "secure", "sameSite", "same_site"}  # fmt: skip
            return [
                {k: v for k, v in c.items() if k in keep} if isinstance(c, dict) else c
                for c in value
            ]
        return value


class RenderAuth(_Model):
    """Credentials for one render. Nothing here is shared with other renders."""

    cookies: list[Cookie] = Field(default_factory=list, max_length=MAX_COOKIES)
    storage_state: StorageState | None = None
    headers: dict[str, dict[str, SecretStr]] = Field(default_factory=dict, max_length=MAX_ORIGINS)
    """Extra request headers per exact origin, e.g.
    ``{"https://api.example.com": {"Authorization": "Bearer ..."}}``."""

    @field_validator("headers")
    @classmethod
    def _valid_headers(
        cls, value: dict[str, dict[str, SecretStr]]
    ) -> dict[str, dict[str, SecretStr]]:
        result: dict[str, dict[str, SecretStr]] = {}
        for origin, headers in value.items():
            canonical = canonical_origin(origin)
            if canonical == BUNDLE_ORIGIN:
                raise ValueError(
                    f"{canonical} is served from memory, so headers for it are never sent"
                )
            if canonical in result:
                raise ValueError(f"origin {canonical} is listed twice")
            if len(headers) > MAX_HEADERS_PER_ORIGIN:
                raise ValueError(f"more than {MAX_HEADERS_PER_ORIGIN} headers for {canonical}")
            checked: dict[str, SecretStr] = {}
            for name, secret in headers.items():
                lowered = name.lower()
                if not _TOKEN.match(name):
                    raise ValueError(f"invalid header name {name!r} for {canonical}")
                if lowered in FORBIDDEN_HEADERS:
                    hint = " (use cookies or storage_state)" if "cookie" in lowered else ""
                    raise ValueError(f"header {name!r} can't be set{hint}")
                if lowered in checked:
                    raise ValueError(f"header {name!r} is listed twice for {canonical}")
                checked[lowered] = _check_secret(
                    secret, limit=MAX_HEADER_VALUE, what=f"value of header {name!r}"
                )
            result[canonical] = checked
        return result

    @model_validator(mode="after")
    def _total_size(self) -> RenderAuth:
        total = sum(len(c.value.get_secret_value()) for c in self.all_cookies())
        total += sum(len(v.get_secret_value()) for hs in self.headers.values() for v in hs.values())
        if self.storage_state is not None:
            total += sum(o.secret_size() for o in self.storage_state.origins)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"credentials are larger than {MAX_TOTAL_BYTES} bytes in total")
        return self

    # ------------------------------------------------------------------ use

    def all_cookies(self) -> list[Cookie]:
        extra = self.storage_state.cookies if self.storage_state is not None else []
        return [*self.cookies, *extra]

    def playwright_cookies(self) -> list[dict[str, Any]]:
        """For ``context.add_cookies()``; contains secret values."""
        return [c.to_playwright() for c in self.all_cookies()]

    def playwright_storage_state(self) -> dict[str, Any] | None:
        """For ``new_context(storage_state=...)``: localStorage and IndexedDB (cookies
        go through ``add_cookies``, which accepts url-scoped cookies). Contains secrets."""
        if self.storage_state is None or not self.storage_state.origins:
            return None
        origins: list[dict[str, Any]] = []
        for o in self.storage_state.origins:
            origin: dict[str, Any] = {
                "origin": o.origin,
                "localStorage": [
                    {"name": i.name, "value": i.value.get_secret_value()} for i in o.local_storage
                ],
            }
            if o.indexed_db is not None:
                origin["indexedDB"] = o.indexed_db.get_secret_value()
            origins.append(origin)
        return {"cookies": [], "origins": origins}

    def headers_for(self, url: str) -> dict[str, str]:
        """Configured headers for the exact origin of ``url`` (lowercase names).
        Contains secret values; pass straight to the request."""
        origin = origin_of(url)
        headers = self.headers.get(origin) if origin is not None else None
        if not headers:
            return {}
        return {name: secret.get_secret_value() for name, secret in headers.items()}

    @property
    def has_headers(self) -> bool:
        return any(self.headers.values())
