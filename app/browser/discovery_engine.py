"""Staged, safe discovery of how a single page works.

The engine reads a page through the live :class:`PlaywrightController` and
distils it into a structured *discovery* dict that
:class:`~app.browser.website_skills.WebsiteSkillManager` turns into a route
skill. It runs in stages (static -> navigation -> interaction -> workflow) and is
deliberately **non-destructive**: it never clicks, submits, deletes, or buys.
Stage 3 detects the *presence* of interactive widgets from the DOM/accessibility
tree rather than exercising them — the safe minimum.

# ponytail: detection-only interaction discovery (no real clicks). Upgrade path:
# drive safe widgets (tabs/accordions) through the controller if richer skills
# are needed — but that surface is risky, so it stays opt-out by construction.

Pure analysis over already-extracted data keeps this importable and unit-testable
without a browser (see ``classifyDiscovery`` / ``inferWorkflows`` below).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

# Words that mark a control as destructive/sensitive. Discovery never *acts*, but
# flags these so a driving agent treads carefully (req: Safety).
DANGER_WORDS = (
    "delete", "remove", "purchase", "buy now", "pay", "payment", "checkout",
    "logout", "log out", "sign out", "deactivate", "close account",
    "cancel subscription", "unsubscribe", "confirm", "destroy", "wipe",
)

# Widget signatures detected by role/class/text — presence only, never exercised.
_WIDGET_HINTS = {
    "tabs": ("tab", "tablist"),
    "dropdowns": ("dropdown", "menu", "combobox", "select"),
    "accordions": ("accordion", "collapse", "expander"),
    "dialogs": ("dialog", "modal"),
    "pagination": ("pagination", "pager", "next page", "page-"),
    "search": ("search",),
    "filters": ("filter", "facet", "sort"),
}


def _danger(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in DANGER_WORDS)


def routeOf(url: str) -> str:
    """Normalised path used as a route key — query/fragment dropped."""
    path = urlsplit(url or "").path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def classifyNavigation(links: list[dict[str, Any]], pageUrl: str) -> dict[str, Any]:
    """Split links into internal routes vs external, deduped by route."""
    host = urlsplit(pageUrl or "").netloc
    internal: dict[str, str] = {}
    external: list[str] = []
    for link in links:
        href = (link or {}).get("href") or ""
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        parts = urlsplit(href)
        if parts.netloc and parts.netloc != host:
            external.append(href)
            continue
        route = routeOf(href)
        internal.setdefault(route, (link.get("text") or "").strip()[:80])
    routes = [{"route": r, "text": t} for r, t in sorted(internal.items())]
    return {"internalRoutes": routes, "externalCount": len(set(external))}


def detectWidgets(blob: str) -> list[str]:
    """Detect which interactive widget families are present (by keyword)."""
    low = (blob or "").lower()
    found = []
    for name, hints in _WIDGET_HINTS.items():
        if any(h in low for h in hints):
            found.append(name)
    return found


def inferWorkflows(forms: list[dict[str, Any]], nav: dict[str, Any], blob: str) -> list[dict[str, Any]]:
    """Infer reusable task flows from forms, navigation, and visible text."""
    low = (blob or "").lower()
    routes = " ".join(r["route"] for r in nav.get("internalRoutes", [])).lower()
    flows: list[dict[str, Any]] = []

    def hasField(form, *types):
        return any((f.get("type") or "") in types for f in form.get("fields", []))

    for form in forms:
        if hasField(form, "password"):
            flows.append({"name": "Login", "trigger": "password form",
                          "steps": ["Open page", "Enter credentials", "Submit"]})
        if hasField(form, "search") or "search" in (form.get("name") or "").lower():
            flows.append({"name": "Search", "trigger": "search form",
                          "steps": ["Focus search", "Type query", "Submit"]})
        if hasField(form, "file"):
            flows.append({"name": "Upload", "trigger": "file input",
                          "steps": ["Open page", "Choose file", "Submit"]})
    if any(k in routes or k in low for k in ("cart", "checkout", "basket")):
        flows.append({"name": "Checkout", "trigger": "cart/checkout link",
                      "steps": ["Add to cart", "Open cart", "Checkout", "Pay"],
                      "warning": "Involves payment — never auto-complete."})
    if any(k in routes for k in ("/signup", "/register", "/join")):
        flows.append({"name": "Signup", "trigger": "signup route",
                      "steps": ["Open signup", "Fill details", "Submit"]})
    # Dedup by name (first wins — keeps the most specific trigger seen).
    seen: dict[str, dict[str, Any]] = {}
    for f in flows:
        seen.setdefault(f["name"], f)
    return list(seen.values())


def classifyDiscovery(collected: dict[str, Any]) -> dict[str, Any]:
    """Turn raw collected page data into a structured discovery dict.

    *collected* is what :meth:`DiscoveryEngine._collect` gathers; kept separate so
    the classification logic is pure and testable without a browser.
    """
    url = collected.get("url") or ""
    links = collected.get("links", [])
    buttons = collected.get("buttons", [])
    forms = collected.get("forms", [])
    text = collected.get("text", "")

    nav = classifyNavigation(links, url)
    blob = " ".join([
        text[:4000],
        " ".join(b.get("text") or "" for b in buttons),
        " ".join(json_safe(collected.get("aria", []))),
    ])
    warnings = sorted({b.get("text", "").strip() for b in buttons if _danger(b.get("text", ""))} - {""})

    return {
        "route": routeOf(url),
        "title": collected.get("title") or "",
        "purpose": _guessPurpose(routeOf(url), forms, blob),
        "description": text[:500].strip(),
        "ui": detectWidgets(blob),
        "buttons": [
            {"text": (b.get("text") or "").strip()[:80], "danger": _danger(b.get("text", ""))}
            for b in buttons if (b.get("text") or "").strip()
        ][:40],
        "forms": [
            {
                "action": f.get("action"),
                "method": f.get("method"),
                "fields": [
                    {"name": fl.get("name"), "type": fl.get("type"), "required": fl.get("required")}
                    for fl in f.get("fields", [])
                ],
            }
            for f in forms
        ],
        "navigation": nav["internalRoutes"][:60],
        "redirects": collected.get("redirects", []),
        "workflows": inferWorkflows(forms, nav, blob),
        "warnings": warnings,
        "dynamic": _dynamicNotes(collected),
        "knownProblems": [],
        "imageCount": collected.get("imageCount", 0),
        "externalLinkCount": nav["externalCount"],
    }


def json_safe(items: list[Any]) -> list[str]:
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            out.append(" ".join(str(v) for v in it.values() if isinstance(v, str)))
    return out


def _guessPurpose(route: str, forms: list[dict[str, Any]], blob: str) -> str:
    r = route.lower()
    if any((fl.get("type") == "password") for f in forms for fl in f.get("fields", [])):
        return "Authentication page (login/sign-in)."
    if any(k in r for k in ("login", "signin")):
        return "Login page."
    if any(k in r for k in ("signup", "register", "join")):
        return "Account creation page."
    if any(k in r for k in ("search", "results")):
        return "Search / results page."
    if any(k in r for k in ("checkout", "cart")):
        return "Checkout / cart page."
    if any(k in r for k in ("settings", "account", "profile")):
        return "User settings / profile page."
    if route == "/":
        return "Site home / landing page."
    return "Content page."


def _dynamicNotes(collected: dict[str, Any]) -> list[str]:
    notes = []
    if collected.get("imageCount", 0) > 30:
        notes.append("Image-heavy page (possible lazy-loading / infinite scroll).")
    if any("modal" in w or "dialog" in w for w in detectWidgets(
        " ".join(json_safe(collected.get("aria", []))))):
        notes.append("Uses dialogs/modals — content may appear after interaction.")
    return notes


class DiscoveryEngine:
    """Runs the staged discovery against a live controller (non-destructive)."""

    async def _collect(self, controller: Any) -> dict[str, Any]:
        """Stage 1+2: gather static page data + accessibility tree. No interaction."""
        title = (await controller.getTitle()).get("title")
        url = (await controller.getUrl()).get("url")
        text = (await controller.extractText()).get("text", "")
        links = (await controller.extractLinks()).get("links", [])
        buttons = (await controller.extractButtons()).get("buttons", [])
        forms = (await controller.extractForms()).get("forms", [])
        images = await controller.extractImages()
        aria: list[Any] = []
        try:
            tree = await controller.getAccessibilityTree(interestingOnly=True)
            aria = tree.get("tree", tree.get("nodes", [])) if isinstance(tree, dict) else []
        except Exception:  # noqa: BLE001 - accessibility tree is best-effort
            aria = []
        return {
            "url": url, "title": title, "text": text, "links": links,
            "buttons": buttons, "forms": forms, "imageCount": images.get("count", 0),
            "aria": aria if isinstance(aria, list) else [aria],
        }

    async def discoverPage(self, controller: Any) -> dict[str, Any]:
        """Run all safe stages on the current page and return a discovery dict."""
        collected = await self._collect(controller)
        discovery = classifyDiscovery(collected)
        discovery["stagesRun"] = ["static", "navigation", "interaction(detect)", "workflow"]
        return discovery


_engineSingleton: DiscoveryEngine | None = None


def getDiscoveryEngine() -> DiscoveryEngine:
    global _engineSingleton
    if _engineSingleton is None:
        _engineSingleton = DiscoveryEngine()
    return _engineSingleton


# --------------------------------------------------------------------------- #
# Self-check (ponytail: runnable proof the heuristics hold; `python -m`).
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sample = {
        "url": "https://github.com/login?x=1",
        "title": "Sign in",
        "text": "Sign in to GitHub",
        "links": [
            {"href": "https://github.com/", "text": "Home"},
            {"href": "https://github.com/join", "text": "Sign up"},
            {"href": "https://twitter.com/github", "text": "Twitter"},
        ],
        "buttons": [{"text": "Sign in"}, {"text": "Delete account"}],
        "forms": [{"action": "/session", "method": "post",
                   "fields": [{"name": "login", "type": "text"},
                              {"name": "password", "type": "password"}]}],
        "imageCount": 2,
        "aria": ["search box", "tablist"],
    }
    d = classifyDiscovery(sample)
    assert d["route"] == "/login", d["route"]
    assert "Login" in [w["name"] for w in d["workflows"]]
    assert "Delete account" in d["warnings"]
    assert "search" in d["ui"] and "tabs" in d["ui"]
    assert routeOf("https://x.com/") == "/"
    assert routeOf("https://x.com/a/b/") == "/a/b"
    assert classifyNavigation(sample["links"], sample["url"])["externalCount"] == 1
    print("discovery_engine self-check OK")
