"""Minimal structured tracing: one JSON line per LLM/tool call.

Deliberately dependency-free. When a project needs more, swap the body of
`span` for OpenTelemetry (GenAI semantic conventions) without changing callers.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import Config

TRACE_PATH = Path("runs/trace.jsonl")

# evals/run.py scores cases concurrently (ThreadPoolExecutor); each case's LLM calls append to
# the same trace file from a different thread. A single `write()` of a short line is usually
# atomic enough in practice, but a trace line can run to several KB (a verbose tool-call log), so
# this guards against two threads' lines interleaving into one corrupted line. If you ever run
# eval scoring sequentially instead, this lock is still harmless to keep.
_write_lock = threading.Lock()
_warned = False


def _warn_unwritable(exc: OSError) -> None:
    """One short line per process, naming only the file and the error class: a trace file that
    cannot be written (a read-only disk, a directory the user does not own) must never fail the
    traced call, and must not fill the log either."""
    global _warned
    if _warned:
        return
    _warned = True
    print(
        f"tracing: cannot write {TRACE_PATH.name} ({type(exc).__name__}); traces are skipped",
        file=sys.stderr,
    )


@contextmanager
def span(
    name: str,
    *,
    config: Config | None = None,
    prompt_version: str | None = None,
    **attrs: Any,
) -> Iterator[dict[str, Any]]:
    """Record a span. Callers may add fields (tokens, cost, output) to the yielded dict."""
    cfg = config or Config.from_env()
    record: dict[str, Any] = {"name": name, "prompt_version": prompt_version, **attrs}
    start = time.perf_counter()
    try:
        yield record
        record["status"] = "ok"
    except Exception as exc:
        record["status"] = "error"
        record["error"] = repr(exc)
        raise
    finally:
        record["duration_s"] = round(time.perf_counter() - start, 4)
        if not cfg.tracing_disabled:
            try:
                TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
                with _write_lock, TRACE_PATH.open("a") as f:
                    f.write(json.dumps(record, default=str) + "\n")
            except OSError as exc:
                _warn_unwritable(exc)
