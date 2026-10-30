"""Stage D1, phase 6: the Dockerfile, .dockerignore and fly.toml are checked for obvious mistakes
without running docker or fly."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.shop import deploycheck
from app.shop.deploycheck import check, check_files

ROOT = Path(__file__).resolve().parents[1]
DOCKER = (ROOT / "Dockerfile").read_text()
IGNORE = (ROOT / ".dockerignore").read_text()
FLY = (ROOT / "fly.toml").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()


def problems(docker: str = DOCKER, ignore: str = IGNORE, fly: str = FLY) -> list[str]:
    return check(docker, ignore, fly, PYPROJECT)


def test_the_real_files_have_no_obvious_mistakes() -> None:
    assert check_files() == [] and problems() == []
    assert deploycheck.main() == 0


def test_the_files_say_what_the_brief_asks() -> None:
    assert "USER app" in DOCKER and "APP_DEPLOY_MODE=1" in DOCKER and "app.shop.serve" in DOCKER
    assert "uv sync --frozen --no-dev" in DOCKER
    for needle in (
        'size = "shared-cpu-1x"',
        'memory = "512mb"',
        "force_https = true",
        'path = "/healthz"',
        'auto_stop_machines = "stop"',
        "auto_start_machines = true",
        "internal_port = 8080",
    ):
        assert needle in FLY
    for name in (".env", "shop-data", "evals", "tests", ".git"):
        assert name in IGNORE.splitlines()


@pytest.mark.parametrize(
    ("edit", "expect"),
    [
        (lambda d: d + "\nCOPY . /app\n", "whole build context"),
        (lambda d: d + "\nCOPY .env /app/.env\n", "copies a .env"),
        (lambda d: d + "\nCOPY tests ./tests\n", "does not need"),
        (lambda d: d.replace("USER app", "USER root"), "non-root"),
        (lambda d: d.replace("USER app\n", ""), "non-root"),
        (lambda d: d.replace("APP_DEPLOY_MODE=1", "APP_DEPLOY_MODE=0"), "APP_DEPLOY_MODE"),
        (lambda d: d.replace("--frozen", ""), "--frozen"),
        (lambda d: d.replace(" --no-dev", ""), "dev dependencies"),
        (lambda d: d.replace("app.shop.serve", "app.shop.server"), "app.shop.serve"),
        (lambda d: d.replace("EXPOSE 8080", "EXPOSE 9000"), "EXPOSE (9000)"),
        (lambda d: d.replace("APP_PORT=8080", "APP_PORT=9000"), "APP_PORT in the Dockerfile"),
        (lambda d: d.replace("python:3.14-slim", "python:3.9-slim"), "older than"),
        (lambda d: d + "\nENV ANTHROPIC_API_KEY=sk-ant-x\n", "secret in the image"),
    ],
)
def test_dockerfile_mistakes_are_found(edit: object, expect: str) -> None:
    found = problems(docker=edit(DOCKER))  # type: ignore[operator]
    assert any(expect in p for p in found), found


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("internal_port = 8080", "internal_port = 9090", "does not match"),
        ("force_https = true", "force_https = false", "force https"),
        ('auto_stop_machines = "stop"', 'auto_stop_machines = "off"', "auto-stop"),
        ("auto_start_machines = true", "auto_start_machines = false", "auto-start"),
        ('path = "/healthz"', 'path = "/"', "health check"),
        ('APP_DEPLOY_MODE = "1"', 'APP_DEPLOY_MODE = "0"', "APP_DEPLOY_MODE"),
        ('APP_PORT = "8080"', 'APP_PORT = "9090"\n  APP_ORIGIN_SECRET = "oops"', "secrets belong"),
        ('size = "shared-cpu-1x"', 'size = "performance-2x"', "shared-cpu-1x"),
        ("min_machines_running = 0", "min_machines_running = 3", "more than one machine"),
    ],
)
def test_fly_mistakes_are_found(old: str, new: str, expect: str) -> None:
    assert old in FLY
    found = problems(fly=FLY.replace(old, new))
    assert any(expect in p for p in found), found


def test_dockerignore_mistakes_are_found() -> None:
    for name in (".env", "shop-data", "evals", "tests"):
        trimmed = "\n".join(x for x in IGNORE.splitlines() if x != name)
        assert any(name in p for p in problems(ignore=trimmed)), name
    assert any("not valid TOML" in p for p in problems(fly="not [ toml"))


def test_missing_files_are_reported(tmp_path: Path) -> None:
    assert check_files(tmp_path) == [
        "missing file: Dockerfile",
        "missing file: .dockerignore",
        "missing file: fly.toml",
    ]


def test_the_make_targets_exist_and_do_not_run_docker_or_fly() -> None:
    make = (ROOT / "Makefile").read_text()
    for target in ("deploy-check", "deploy-smoke"):
        body = make.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]
        assert "docker" not in body and "fly " not in body and "APP_LLM_MODE=live" not in body
