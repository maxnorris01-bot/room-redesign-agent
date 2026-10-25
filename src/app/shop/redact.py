"""Safe descriptions of an exception for a trace, a page or a diagnostic.

An exception message can echo a request, a header or a path. `redact` replaces anything that
looks like a key, token or Authorization value with `[redacted]`; `describe_exception` gives the
class, the redacted message (first 300 characters) and the repository file and line of the
innermost frame in `src/` or `scripts/`. Nothing here ever reads the environment or a request body.
"""

from __future__ import annotations

import re
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MESSAGE_CHARS = 300
_PATTERNS = (
    re.compile(r"(?i)(authorization|x-api-key|api[-_ ]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
)


def redact(text: str) -> str:
    """Replace anything that looks like a key, token or Authorization value with `[redacted]`."""
    for pattern in _PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def repo_frame(exc: BaseException) -> str:
    """`path:line` (relative to the repository) of the innermost frame inside src/ or scripts/."""
    found = ""
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            rel = Path(frame.filename).resolve().relative_to(REPO_ROOT)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in ("src", "scripts"):
            found = f"{rel.as_posix()}:{frame.lineno}"
    return found


def describe_exception(exc: BaseException) -> tuple[str, str]:
    """(`ClassName: redacted message`, the repository location), the message cut to 300 chars."""
    message = redact(" ".join(str(exc).split()))[:MESSAGE_CHARS]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__, repo_frame(exc)
