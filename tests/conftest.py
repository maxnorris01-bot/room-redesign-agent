from __future__ import annotations

from pathlib import Path

import pytest

from app import tracing


@pytest.fixture(autouse=True)
def _trace_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test traces out of runs/ and out of any real spike-output/ folder."""
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "trace.jsonl")
