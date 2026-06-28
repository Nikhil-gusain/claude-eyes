"""Browser memory — a small, searchable store of pages the agent has seen.

Re-scraping the same page every time is wasteful; this lets an agent
``remember_page`` (capture its title/URL/structure/screenshot once) and later
``search_memory("pricing page")`` to recall it without navigating again.

The store is a single JSON file (a list of page records). Ranking is plain
keyword scoring over each record's text — deterministic and dependency-free, so
the search logic is unit-testable without a browser or an LLM.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.utils.config import settings
from app.utils.helpers import ensureDir, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.memory")

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _haystack(entry: dict[str, Any]) -> str:
    """Flatten the searchable fields of a record into one lowercase blob."""
    parts = [
        entry.get("title") or "",
        entry.get("url") or "",
        entry.get("host") or "",
        entry.get("summary") or "",
        " ".join(entry.get("tags") or []),
    ]
    return " ".join(parts).lower()


def scoreEntry(entry: dict[str, Any], tokens: list[str]) -> int:
    """Count how many query tokens appear in the record (substring matches)."""
    hay = _haystack(entry)
    return sum(hay.count(tok) for tok in tokens)


def rankEntries(entries: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Return records matching *query*, best first (ties keep insertion order)."""
    tokens = _tokenize(query)
    if not tokens:
        return list(entries)
    scored = [(scoreEntry(e, tokens), i, e) for i, e in enumerate(entries)]
    hits = [(s, i, e) for (s, i, e) in scored if s > 0]
    hits.sort(key=lambda t: (-t[0], t[1]))
    return [{**e, "_score": s} for (s, _i, e) in hits]


class PageMemory:
    """Persistent, keyword-searchable store of remembered pages."""

    def __init__(self, storeFile: Path | str | None = None) -> None:
        self.storeFile: Path = Path(storeFile or settings.memoryFile)
        ensureDir(self.storeFile.parent)

    def _load(self) -> list[dict[str, Any]]:
        if not self.storeFile.exists():
            return []
        try:
            data = json.loads(self.storeFile.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):  # corrupt/unreadable -> start fresh
            logger.warning("Memory store unreadable; treating as empty: %s", self.storeFile)
            return []

    def _save(self, entries: list[dict[str, Any]]) -> None:
        self.storeFile.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def remember(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add (or refresh) a page record, de-duplicated by URL."""
        entries = self._load()
        url = entry.get("url")
        entries = [e for e in entries if e.get("url") != url]
        nextId = max((e.get("id", 0) for e in entries), default=0) + 1
        record = {**entry, "id": nextId, "savedAt": utcTimestamp()}
        entries.append(record)
        self._save(entries)
        logger.info("Remembered page %s (%s) — %d total", record.get("title"), url, len(entries))
        return {"id": nextId, "url": url, "remembered": True, "count": len(entries)}

    def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Return the best-matching remembered pages for *query*."""
        entries = self._load()
        ranked = rankEntries(entries, query)
        return {
            "query": query,
            "results": ranked[: max(0, limit)],
            "matched": len(ranked),
            "total": len(entries),
        }

    def list(self, limit: int = 50) -> dict[str, Any]:
        """Return the most recently remembered pages."""
        entries = self._load()
        return {"pages": list(reversed(entries))[: max(0, limit)], "total": len(entries)}

    def clear(self) -> dict[str, Any]:
        """Forget everything (delete the store)."""
        count = len(self._load())
        if self.storeFile.exists():
            self.storeFile.unlink()
        return {"cleared": count}


_memorySingleton: PageMemory | None = None


def getPageMemory() -> PageMemory:
    """Return the shared :class:`PageMemory`, creating it on first use."""
    global _memorySingleton
    if _memorySingleton is None:
        _memorySingleton = PageMemory()
    return _memorySingleton
