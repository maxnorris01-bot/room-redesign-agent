"""Who is making the current API request (deployment mode); set by the server around one call."""

from __future__ import annotations

from contextvars import ContextVar

current_visitor: ContextVar[str | None] = ContextVar("shop_visitor", default=None)
