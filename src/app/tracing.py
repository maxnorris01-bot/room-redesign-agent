"""Minimal structured tracing: one JSON line per LLM/tool call.

Deliberately dependency-free. When a project needs more, swap the body of
`span` for OpenTelemetry (GenAI semantic conventions) without changing callers.
"""

from __future__ import annotations

import json
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
            TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _write_lock, TRACE_PATH.open("a") as f:
                f.write(json.dumps(record, default=str) + "\n")
