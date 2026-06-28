"""Unit tests for browser memory (the searchable page store) — browser-free."""

from __future__ import annotations

from pathlib import Path

from app.browser.page_memory import PageMemory, rankEntries


def _mem(tmp_path: Path) -> PageMemory:
    return PageMemory(storeFile=tmp_path / "pages.json")


def test_rememberAndSearch(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    mem.remember({"url": "https://shop.com/pricing", "title": "Pricing Plans",
                  "summary": "monthly and annual pricing tiers", "tags": ["pricing"]})
    mem.remember({"url": "https://shop.com/about", "title": "About Us",
                  "summary": "our company story", "tags": ["company"]})

    hit = mem.search("pricing")
    assert hit["matched"] == 1
    assert hit["results"][0]["url"] == "https://shop.com/pricing"
    assert hit["total"] == 2


def test_dedupeByUrl(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    mem.remember({"url": "https://x.com", "title": "First", "summary": "v1"})
    mem.remember({"url": "https://x.com", "title": "Second", "summary": "v2"})
    listing = mem.list()
    assert listing["total"] == 1
    assert listing["pages"][0]["title"] == "Second"


def test_idsAreMonotonicAfterDedupe(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    a = mem.remember({"url": "https://a.com", "title": "A"})
    b = mem.remember({"url": "https://b.com", "title": "B"})
    assert b["id"] > a["id"]
    # Re-remembering A removes the old A then re-adds with a fresh, higher id.
    a2 = mem.remember({"url": "https://a.com", "title": "A2"})
    assert a2["id"] > b["id"]


def test_clear(tmp_path: Path) -> None:
    mem = _mem(tmp_path)
    mem.remember({"url": "https://a.com", "title": "A"})
    assert mem.clear()["cleared"] == 1
    assert mem.list()["total"] == 0


def test_rankingOrdersByScore() -> None:
    entries = [
        {"url": "u1", "title": "login form", "summary": "login here"},
        {"url": "u2", "title": "home", "summary": "welcome"},
        {"url": "u3", "title": "login page", "summary": "login login login"},
    ]
    ranked = rankEntries(entries, "login")
    assert [e["url"] for e in ranked] == ["u3", "u1"]  # u3 has more 'login' hits; u2 excluded


def test_emptyQueryReturnsAll() -> None:
    entries = [{"url": "u1", "title": "a"}, {"url": "u2", "title": "b"}]
    assert len(rankEntries(entries, "")) == 2


def test_corruptStoreTreatedAsEmpty(tmp_path: Path) -> None:
    store = tmp_path / "pages.json"
    store.write_text("not json{", encoding="utf-8")
    mem = PageMemory(storeFile=store)
    assert mem.list()["total"] == 0
    # And it can still remember afterwards (overwrites the corrupt file).
    mem.remember({"url": "https://a.com", "title": "A"})
    assert mem.list()["total"] == 1
