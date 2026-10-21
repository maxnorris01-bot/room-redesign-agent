"""Load versioned prompts from the `prompts/` directory.

Prompt files start with a small front-matter block:

    ---
    version: 1
    ---
    <prompt text>
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


def _parse(name: str, raw: str) -> Prompt:
    version = "unversioned"
    text = raw
    if raw.startswith("---\n"):
        header, _, body = raw[4:].partition("\n---\n")
        for line in header.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "version":
                version = value.strip()
        text = body
    return Prompt(name=name, version=version, text=text.strip())


def load_prompt(name: str, version: str | None = None) -> Prompt:
    """Load `prompts/<name>.md` (the current version), or a specific `version`.

    An older version is kept beside the current one as `<name>.v<version>.md` so runs can compare
    them (for example `perception.v1.md`). Asking for a version that does not exist raises.
    """
    current = _parse(name, (PROMPTS_DIR / f"{name}.md").read_text())
    if version is None or version == current.version:
        return current
    old = PROMPTS_DIR / f"{name}.v{version}.md"
    if not old.is_file():
        raise FileNotFoundError(
            f"no version {version} of prompt {name!r} (current is {current.version})"
        )
    loaded = _parse(name, old.read_text())
    if loaded.version != version:
        raise ValueError(f"{old.name} declares version {loaded.version}, expected {version}")
    return loaded
