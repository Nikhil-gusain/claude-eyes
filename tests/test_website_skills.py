"""Unit tests for the Website Skill System — browser-free.

Covers the pure discovery heuristics, the merge/render/parse round-trip, and the
WebsiteSkillManager's index lookups, confidence, modes, and export — all without
a real browser or an LLM (the logic is deterministic by design).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.browser.discovery_engine import (
    classifyDiscovery,
    classifyNavigation,
    inferWorkflows,
    routeOf,
)
from app.browser.website_skills import (
    LEARN,
    OFF,
    READ_ONLY,
    WebsiteSkillManager,
    domainOf,
    mergeDiscovery,
    parseRouteMarkdown,
    renderRouteMarkdown,
)

LOGIN = {
    "url": "https://github.com/login?return=1",
    "title": "Sign in",
    "text": "Sign in to GitHub",
    "links": [
        {"href": "https://github.com/", "text": "Home"},
        {"href": "https://github.com/join", "text": "Sign up"},
        {"href": "https://twitter.com/x", "text": "Twitter"},
    ],
    "buttons": [{"text": "Sign in"}, {"text": "Delete account"}],
    "forms": [{"action": "/session", "method": "post",
               "fields": [{"name": "login", "type": "text"},
                          {"name": "password", "type": "password"}]}],
    "imageCount": 1,
    "aria": ["search box"],
}


def test_routeAndDomainNormalisation() -> None:
    assert routeOf("https://x.com/") == "/"
    assert routeOf("https://x.com/a/b/?q=1") == "/a/b"
    assert domainOf("https://www.GitHub.com/login") == "github.com"


def test_classifyDiscovery_inference_and_safety() -> None:
    d = classifyDiscovery(LOGIN)
    assert d["route"] == "/login"
    assert "Login" in [w["name"] for w in d["workflows"]]
    assert "Delete account" in d["warnings"]  # destructive control flagged, not pressed
    assert classifyNavigation(LOGIN["links"], LOGIN["url"])["externalCount"] == 1


def test_checkout_workflow_carries_payment_warning() -> None:
    nav = {"internalRoutes": [{"route": "/cart", "text": "Cart"}]}
    flows = inferWorkflows([], nav, "view cart and checkout")
    checkout = next(f for f in flows if f["name"] == "Checkout")
    assert "warning" in checkout


def test_merge_appends_and_versions_without_overwrite() -> None:
    m1 = mergeDiscovery(None, classifyDiscovery(LOGIN), LEARN)
    assert m1["discoveryVersion"] == 1 and len(m1["history"]) == 1
    m2 = mergeDiscovery(m1, {"route": "/login", "buttons": [{"text": "Google login"}]}, LEARN)
    assert m2["discoveryVersion"] == 2
    texts = [b["text"] for b in m2["buttons"]]
    assert "Sign in" in texts and "Google login" in texts  # accumulated, not replaced
    assert m2["purpose"] == m1["purpose"]  # prior knowledge retained


def test_markdown_roundtrip() -> None:
    data = mergeDiscovery(None, classifyDiscovery(LOGIN), LEARN)
    md = renderRouteMarkdown(data)
    assert "## Discovery history" in md and "⚠️ destructive" in md
    assert parseRouteMarkdown(md)["discoveryVersion"] == 1
    assert parseRouteMarkdown("# hand-written, no data block") is None


def test_manager_lookup_is_route_scoped(tmp_path: Path) -> None:
    mgr = WebsiteSkillManager(root=tmp_path, mode=LEARN)
    mgr.saveDiscovery(LOGIN["url"], classifyDiscovery(LOGIN))
    loaded = mgr.loadForUrl("https://github.com/login")
    assert loaded["known"] and loaded["routeKnown"]
    assert loaded["skill"]["route"] == "/login"
    # An unvisited route on a known site: site known, route not.
    other = mgr.loadForUrl("https://github.com/settings")
    assert other["known"] and other["routeKnown"] is False


def test_confidence_drives_rediscovery_flag(tmp_path: Path) -> None:
    mgr = WebsiteSkillManager(root=tmp_path, mode=LEARN)
    mgr.saveDiscovery(LOGIN["url"], classifyDiscovery(LOGIN))
    for _ in range(2):
        mgr.recordFailure("https://github.com/login")  # 70 -> 50 -> 30
    out = mgr.loadForUrl("https://github.com/login")
    assert out["needsRediscovery"] is True


def test_isolation_between_domains(tmp_path: Path) -> None:
    mgr = WebsiteSkillManager(root=tmp_path, mode=LEARN)
    mgr.saveDiscovery(LOGIN["url"], classifyDiscovery(LOGIN))
    mgr.saveDiscovery("https://reddit.com/r/python", {"route": "/r/python", "title": "Python"})
    assert (tmp_path / "github.com").exists() and (tmp_path / "reddit.com").exists()
    assert mgr.loadForUrl("https://reddit.com/login")["routeKnown"] is False


def test_modes_gate_reads_and_writes(tmp_path: Path) -> None:
    learn = WebsiteSkillManager(root=tmp_path, mode=LEARN)
    learn.saveDiscovery(LOGIN["url"], classifyDiscovery(LOGIN))

    ro = WebsiteSkillManager(root=tmp_path, mode=READ_ONLY)
    assert ro.loadForUrl("https://github.com/login")["known"]
    with pytest.raises(PermissionError):
        ro.saveDiscovery("https://github.com/x", {"route": "/x"})

    off = WebsiteSkillManager(root=tmp_path, mode=OFF)
    assert off.loadForUrl("https://github.com/login")["known"] is False


def test_export_import_roundtrip(tmp_path: Path) -> None:
    src = WebsiteSkillManager(root=tmp_path / "a", mode=LEARN)
    src.saveDiscovery(LOGIN["url"], classifyDiscovery(LOGIN))
    bundle = src.exportSkills()
    assert "github.com" in bundle["domains"]

    dst = WebsiteSkillManager(root=tmp_path / "b", mode=LEARN)
    res = dst.importSkills(bundle)
    assert res["imported"] > 0
    assert dst.loadForUrl("https://github.com/login")["routeKnown"]
