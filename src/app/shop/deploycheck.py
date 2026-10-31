"""Obvious-mistake checks for the Dockerfile, .dockerignore and fly.toml, without running docker or
fly (`make deploy-check`). Each problem is a plain sentence; an empty list means nothing obvious is
wrong. It cannot prove the image builds or the app starts: the runbook's smoke test does that.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SECRETISH = re.compile(r"(KEY|SECRET|PASSWORD|TOKEN|DATABASE_URL)", re.IGNORECASE)
MUST_IGNORE = (".env", "shop-data", "tests", "evals", ".git", "spike-photos", "runs")


def _dockerfile_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def check(dockerfile: str, dockerignore: str, fly: str, pyproject: str = "") -> list[str]:
    problems: list[str] = []
    lines = _dockerfile_lines(dockerfile)
    joined = "\n".join(lines)
    for ln in lines:
        low = ln.lower()
        if re.match(r"(copy|add)\s+(--\S+\s+)*\.\s", low) or re.match(
            r"(copy|add)\s+(--\S+\s+)*\.$", low
        ):
            problems.append(f"Dockerfile copies the whole build context ({ln!r}); copy named paths")
        if low.startswith(("copy", "add")) and re.search(r"(^|[\s/])\.env\b", low):
            problems.append(f"Dockerfile copies a .env file ({ln!r})")
        if (
            re.search(r"\b(copy|add)\b.*\b(tests|evals|shop-data|spike-photos|runs)\b", low)
            and "--from" not in low
        ):
            problems.append(f"Dockerfile copies something the service does not need ({ln!r})")
        if re.search(r"(ANTHROPIC|SERPER|SERPAPI)_API_KEY\s*=\s*\S", ln) or "DATABASE_URL=" in ln:
            problems.append("Dockerfile sets a secret in the image")
    if not any(
        ln.upper().startswith("USER ") and ln.split()[1] not in ("root", "0") for ln in lines
    ):
        problems.append("Dockerfile does not switch to a non-root USER")
    if "APP_DEPLOY_MODE=1" not in joined.replace(" ", ""):
        problems.append("Dockerfile does not set APP_DEPLOY_MODE=1")
    if "APP_TRACING_DISABLED=1" not in joined.replace(" ", ""):
        problems.append("Dockerfile does not set APP_TRACING_DISABLED=1 (a trace file is written)")
    if not re.search(r"^RUN\b.*\bchown\b.*\bapp:app\b.*\s/app\b", joined, flags=re.MULTILINE):
        problems.append(
            "Dockerfile does not make the app user own /app itself (chown app:app /app)"
        )
    if not re.search(r"app\.shop\.serve\b", joined):
        problems.append("Dockerfile does not start app.shop.serve")
    if "--frozen" not in joined:
        problems.append("Dockerfile installs without --frozen (the lock file would be ignored)")
    if " --no-dev" not in joined and "--no-dev" not in joined:
        problems.append("Dockerfile installs the dev dependencies")
    base = re.search(r"^FROM\s+python:(\d+)\.(\d+)", dockerfile, flags=re.MULTILINE)
    need = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', pyproject)
    if (
        base
        and need
        and (int(base.group(1)), int(base.group(2))) < (int(need.group(1)), int(need.group(2)))
    ):
        problems.append("the Python base image is older than pyproject's requires-python")
    exposed = re.search(r"^EXPOSE\s+(\d+)", dockerfile, flags=re.MULTILINE)
    port = re.search(r"APP_PORT=(\d+)", dockerfile.replace(" ", ""))

    ignored = {ln.strip().rstrip("/") for ln in dockerignore.splitlines() if ln.strip()}
    for name in MUST_IGNORE:
        if name not in ignored:
            problems.append(f".dockerignore does not list {name}")
    if ".env.*" not in ignored and ".env*" not in ignored:
        problems.append(".dockerignore does not list .env.*")

    try:
        conf = tomllib.loads(fly)
    except tomllib.TOMLDecodeError:
        return [*problems, "fly.toml is not valid TOML"]
    service = conf.get("http_service", {})
    env = conf.get("env", {})
    internal = service.get("internal_port")
    for source, value in (
        ("EXPOSE", exposed.group(1) if exposed else None),
        ("APP_PORT in the Dockerfile", port.group(1) if port else None),
        ("APP_PORT in fly.toml", env.get("APP_PORT")),
    ):
        if value is not None and str(value) != str(internal):
            problems.append(
                f"{source} ({value}) does not match fly.toml internal_port ({internal})"
            )
    if str(env.get("APP_TRACING_DISABLED")) != "1":
        problems.append("fly.toml [env] does not set APP_TRACING_DISABLED to 1")
    if not service.get("force_https"):
        problems.append("fly.toml does not force https")
    if service.get("auto_stop_machines") in (None, False, "off"):
        problems.append("fly.toml does not auto-stop idle machines")
    if service.get("auto_start_machines") is not True:
        problems.append("fly.toml does not auto-start machines on a request")
    checks = service.get("checks", [])
    if not any(c.get("path") == "/healthz" for c in checks):
        problems.append("fly.toml has no health check on /healthz")
    if str(env.get("APP_DEPLOY_MODE")) != "1":
        problems.append("fly.toml does not set APP_DEPLOY_MODE=1")
    for name in env:
        if SECRETISH.search(name):
            problems.append(f"fly.toml [env] holds {name}: secrets belong in `fly secrets set`")
    vms = conf.get("vm", [])
    if not vms or vms[0].get("size") != "shared-cpu-1x" or not str(vms[0].get("memory", "")):
        problems.append("fly.toml does not ask for one shared-cpu-1x machine with a memory size")
    if service.get("min_machines_running", 0) > 1:
        problems.append("fly.toml keeps more than one machine running (the service is one machine)")
    return problems


def check_files(root: Path = ROOT) -> list[str]:
    def read(name: str) -> str:
        path = root / name
        return path.read_text() if path.is_file() else ""

    missing = [n for n in ("Dockerfile", ".dockerignore", "fly.toml") if not (root / n).is_file()]
    if missing:
        return [f"missing file: {n}" for n in missing]
    return check(
        read("Dockerfile"), read(".dockerignore"), read("fly.toml"), read("pyproject.toml")
    )


def main() -> int:
    problems = check_files()
    for line in problems:
        print(f"PROBLEM: {line}")
    print(
        "deploy files: "
        + ("no obvious mistakes" if not problems else f"{len(problems)} problem(s)")
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
