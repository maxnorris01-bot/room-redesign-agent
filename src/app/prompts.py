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


def load_prompt(name: str) -> Prompt:
    raw = (PROMPTS_DIR / f"{name}.md").read_text()
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
