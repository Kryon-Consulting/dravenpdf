"""Passwords and passphrases arrive as ``str`` or :class:`pydantic.SecretStr`."""

from __future__ import annotations

from typing import overload

from pydantic import SecretStr


@overload
def reveal(value: str | SecretStr) -> str: ...
@overload
def reveal(value: str | SecretStr | None) -> str | None: ...
def reveal(value: str | SecretStr | None) -> str | None:
    """The plain value, at the point it is handed to pikepdf or pyHanko."""
    return value.get_secret_value() if isinstance(value, SecretStr) else value
