"""Stage D1.1: a trace file that cannot be written never fails a call, and a failed job or an
unexpected server error leaves ONE redacted line on stderr (no ids, bodies, cookies or traces)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app import tracing
from app.config import Config
from app.shop.redact import log_failure
from app.shop.service import ApiError, ShopService


@pytest.fixture
def unwritable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    if os.access(locked, os.W_OK):  # running as a user who ignores permissions
        locked.chmod(0o700)
        pytest.skip("this user can write to a read-only directory")
    monkeypatch.setattr(tracing, "TRACE_PATH", locked / "runs" / "trace.jsonl")
    monkeypatch.setattr(tracing, "_warned", False)
    yield locked
    locked.chmod(0o700)


def test_an_unwritable_trace_path_never_fails_the_call_and_warns_once(
    unwritable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = Config(tracing_disabled=False)
    for _ in range(3):
        with tracing.span("probe", config=cfg) as record:
            record["x"] = 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "trace.jsonl" in err[0] and "PermissionError" in err[0]
    assert str(unwritable) not in err[0]


def test_a_real_error_in_the_traced_body_still_propagates(
    unwritable: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="boom"):
        with tracing.span("probe", config=Config(tracing_disabled=False)):
            raise ValueError("boom")
    assert "tracing:" in capsys.readouterr().err


def test_a_writable_trace_path_still_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "runs" / "trace.jsonl")
    with tracing.span("probe", config=Config(tracing_disabled=False)):
        pass
    line = json.loads((tmp_path / "runs" / "trace.jsonl").read_text())
    assert line["name"] == "probe" and capsys.readouterr().err == ""


def test_a_failing_job_logs_one_line_with_class_and_location(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.shop.store import SessionStore

    try:
        SessionStore(tmp_path / "d").load("../not-a-session")  # fails inside src/
    except Exception as exc:
        status = ShopService._job_failure(exc, "search")
    assert status == (500, "Something went wrong; try again")
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("shop error [search] ") and "at src/app/shop/store.py:" in lines[0]


def test_the_log_line_is_redacted_and_cut(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "sk-ant-abcdef1234567890"
    url = "https://serpapi.com/search?api_key=ZZZZ1234&q=x"
    try:
        raise RuntimeError(f"failed {url} with {secret} " + "x" * 400)
    except RuntimeError as exc:
        log_failure("shop_again", exc)
    out = capsys.readouterr().err
    assert len(out.strip().splitlines()) == 1
    assert "ZZZZ1234" not in out and secret not in out and "serpapi" not in out
    assert "[redacted]" in out and "Traceback" not in out and len(out) < 520


def test_known_api_errors_below_500_do_not_log(capsys: pytest.CaptureFixture[str]) -> None:
    assert ShopService._job_failure(ApiError(429, "cap"), "search") == (429, "cap")
    assert ShopService._job_failure(ApiError(404, "gone"), "search") == (404, "gone")
    assert capsys.readouterr().err == ""


def test_an_unexpected_handler_error_is_one_redacted_line_and_a_generic_500(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_shop_deploy_api import call, make_app

    app = make_app(tmp_path)

    def explode(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db at https://u:p@host/x?token=abcd1234 failed, cookie shop_login=ok")

    monkeypatch.setattr(app, "_call", explode)
    resp = call(app, "GET", "/api/shop/session?id=SECRETSESSIONID")
    assert resp.status == 500
    body = json.loads(resp.body)["error"]
    assert body["code"] == "server_error" and "RuntimeError" not in resp.body.decode()
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("shop error [session] RuntimeError")
    for leak in ("SECRETSESSIONID", "abcd1234", "u:p@host", "shop_login=ok", "Traceback"):
        assert leak not in lines[0]


def test_a_known_api_error_in_a_handler_does_not_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_shop_deploy_api import call, make_app

    resp = call(make_app(tmp_path), "GET", "/api/shop/job/nope")
    assert resp.status == 404 and capsys.readouterr().err == ""
