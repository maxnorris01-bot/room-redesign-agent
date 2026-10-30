"""Who is making the current API request (deployment mode); set by the server around one call."""

from __future__ import annotations

from contextvars import ContextVar

current_invite: ContextVar[str | None] = ContextVar("shop_invite", default=None)
