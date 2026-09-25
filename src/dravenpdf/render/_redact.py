"""Keep request headers (and so credentials) out of logs and error messages.

Playwright appends a "Call log" to many error messages, and for requests it lists
every header it sent, including ``authorization`` and ``cookie`` values. Anything
we log or put into an exception goes through :func:`safe_message` first.
"""

from __future__ import annotations

_CALL_LOG = "\nCall log:"


def safe_message(error: BaseException | str) -> str:
    """The error's first part, without Playwright's call log."""
    text = error if isinstance(error, str) else (getattr(error, "message", None) or str(error))
    return text.split(_CALL_LOG, 1)[0].strip()
