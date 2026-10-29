"""Record and replay Serper responses for the eval only (stage 2r).

`EVAL_SHOP_SERPER_CACHE` picks the mode:

    record  every live Serper response is saved (the default for `make eval-shop-live`)
    use     a saved response younger than 14 days is used and costs no credits; a miss goes live
            and is saved
    replay  `make eval-shop-replay`: only saved responses, no network; a miss is an error
    off     the plain live provider

The app's own runtime never reads this cache: `CachingSerper` is built by the eval runner only.
A file under `evals/shop/cache/serper/` (gitignored: the repo is public and a response is real
Google Shopping data) holds the request, a timestamp and the response body, never a header or a
key. Its name is a hash of the request (URL, query, page, country and language).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.shopping import (
    DEFAULT_GL,
    DEFAULT_HL,
    ProviderError,
    SearchCandidate,
    SerperSearch,
    parse_serper_search,
)
from evals.shop.spec import SHOP_DIR

CACHE_DIR = SHOP_DIR / "cache" / "serper"
MAX_AGE_DAYS = 14
MODES = ("record", "use", "replay", "off")
ENV_VAR = "EVAL_SHOP_SERPER_CACHE"
EMPTY_HELP = "run a live eval once with recording (make eval-shop-live, mode record)"
CACHED_POOL_NOTE = "cached pool, not a fresh-world run"


def cache_mode(environ: dict[str, str] | os._Environ[str], default: str = "off") -> str:
    """The mode named by the environment (an unknown word is an error, not silently off)."""
    mode = (environ.get(ENV_VAR) or default).strip().lower()
    if mode not in MODES:
        raise ValueError(f"{ENV_VAR} must be one of {', '.join(MODES)} (got {mode!r})")
    return mode


@dataclass
class CacheStats:
    mode: str = "off"
    hits: int = 0
    misses: int = 0
    recorded: int = 0
    stale: int = 0  # saved responses ignored for age
    credits_saved: float = 0.0
    missing: list[str] = field(default_factory=list)  # replay: requests the cache did not hold
    _reported: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take_saved(self) -> float:
        """The credits saved since the last call (the runner charges each round for what it spent)."""
        with self._lock:
            out = self.credits_saved - self._reported
            self._reported = self.credits_saved
            return out

    def as_dict(self) -> dict[str, Any]:
        note = CACHED_POOL_NOTE if self.hits else ""
        return {
            "mode": self.mode, "hits": self.hits, "misses": self.misses, "recorded": self.recorded,
            "stale_ignored": self.stale, "credits_saved": self.credits_saved,
            "missing": list(self.missing), "note": note,
        }  # fmt: skip


def request_key(body: dict[str, Any], url: str = SerperSearch.URL) -> str:
    text = json.dumps({"url": url, "body": body}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:40]


class CachingSerper(SerperSearch):
    """`SerperSearch` that saves and replays its responses (see the module docstring)."""

    def __init__(
        self,
        api_key: str,
        stats: CacheStats,
        directory: Path = CACHE_DIR,
        credits_per_call: float = 2.0,
        now: Any = time.time,
    ) -> None:
        # a replay needs no key (nothing is sent); a placeholder keeps the base class happy
        super().__init__(api_key or "cache-only")
        self.stats = stats
        self.directory = directory
        self.credits_per_call = credits_per_call
        self._now = now
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def _read(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text())
            age_days = (self._now() - float(entry["recorded_epoch"])) / 86400
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if age_days > MAX_AGE_DAYS:
            with self._lock:
                self.stats.stale += 1
            return None
        data = entry.get("response")
        return data if isinstance(data, dict) else None

    def _write(self, key: str, body: dict[str, Any], data: dict[str, Any]) -> None:
        write_entry(self.directory, key, body, data, self._now())
        with self._lock:
            self.stats.recorded += 1

    def search(
        self, query: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL, page: int = 1
    ) -> list[SearchCandidate]:
        body: dict[str, Any] = {"q": query, "gl": gl, "hl": hl}
        if page > 1:
            body["page"] = page
        key = request_key(body)
        mode = self.stats.mode
        if mode in ("use", "replay"):
            hit = self._read(key)
            if hit is not None:
                with self._lock:
                    self.stats.hits += 1
                    self.stats.credits_saved += self.credits_per_call
                return parse_serper_search(hit)
            if mode == "replay":
                with self._lock:
                    self.stats.misses += 1
                    self.stats.missing.append(f"{query} (page {page})")
                raise ProviderError("serper", "not in the saved responses (replay mode)")
        with self._lock:
            self.stats.misses += 1
        headers = {"X-API-KEY": self._api_key, "Content-Type": "application/json"}
        data = self._request(lambda: self._client.post(self.URL, json=body, headers=headers))
        if mode in ("record", "use"):
            self._write(key, body, data)
        return parse_serper_search(data)


def write_entry(
    directory: Path, key: str, body: dict[str, Any], data: dict[str, Any], epoch: float
) -> None:
    """Save one response: the request (no header, no key), when it was recorded, the body."""
    directory.mkdir(parents=True, exist_ok=True)
    entry = {
        "request": {"url": SerperSearch.URL, **body},
        "recorded_at": datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds"),
        "recorded_epoch": epoch,
        "response": data,
    }
    tmp = directory / f"{key}.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False))
    tmp.replace(directory / f"{key}.json")


def cache_entries(directory: Path = CACHE_DIR) -> int:
    return len(list(directory.glob("*.json"))) if directory.is_dir() else 0
