"""Website Skill System — persistent, self-improving operational knowledge.

Unlike page memory (which stores *what a page was*), website skills store *how a
site works* and grow more accurate with every visit. Each domain gets its own
folder; a master ``websites.json`` gives O(1) lookup so the AI never scans the
filesystem. Within a domain, ``index.json`` lists routes/workflows and each
route is exactly one markdown file with an embedded machine-readable block so
knowledge can be **merged and versioned, never overwritten**.

Layout::

    website_skills/
        websites.json                 # master index (domain -> folder/index)
        github.com/
            index.json                # routes[] + workflows[]
            routes/login.md           # one skill per route (+ embedded data)
            workflows/login.md
            assets/

Modes (req): ``OFF`` (no read/write), ``READ_ONLY`` (read but never modify), and
``LEARN`` (read + discover + update + create — the default).

The store is plain JSON + markdown with deterministic, dependency-free logic, so
the merge/render/index code is unit-testable without a browser or an LLM (see the
``__main__`` self-check). File writes are guarded by a process-wide lock.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.browser.discovery_engine import routeOf
from app.utils.config import settings
from app.utils.helpers import ensureDir, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.skills")

OFF, READ_ONLY, LEARN = "OFF", "READ_ONLY", "LEARN"
MODES = (OFF, READ_ONLY, LEARN)

# Default confidence for a freshly discovered route; bumps on success, drops on
# failure, and a route below the configured threshold is auto-rediscovered.
_DEFAULT_CONFIDENCE = 70
_DATA_MARKER = "<!-- ABC-SKILL-DATA -->"
_DATA_RE = re.compile(r"<!-- ABC-SKILL-DATA -->\s*```json\s*(\{.*?\})\s*```", re.DOTALL)

# List-valued fields that accumulate across discoveries (merged, never replaced).
_LIST_FIELDS = ("ui", "buttons", "forms", "navigation", "redirects", "workflows",
                "warnings", "dynamic", "knownProblems")
# Scalar text fields — the latest non-empty discovery wins.
_TEXT_FIELDS = ("title", "purpose", "description")


def domainOf(url: str) -> str:
    """Bare domain key (``www.`` stripped, lowercased) — folder + index key."""
    host = urlsplit(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _slug(route: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (route or "/").lower()).strip("_")
    return s or "home"


def _dedupe(items: list[Any]) -> list[Any]:
    """Stable de-dup for lists of strings or dicts."""
    seen: set[str] = set()
    out: list[Any] = []
    for it in items or []:
        key = json.dumps(it, sort_keys=True) if isinstance(it, (dict, list)) else str(it)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def renderRouteMarkdown(data: dict[str, Any]) -> str:
    """Render a route skill dict into one markdown file (data embedded at end)."""
    L: list[str] = []
    L.append(f"# {data.get('title') or data.get('route')}")
    L.append("")
    L.append(f"**Route:** `{data.get('route')}`  ")
    L.append(f"**Confidence:** {data.get('confidence', _DEFAULT_CONFIDENCE)}/100  ")
    L.append(f"**Discovery version:** {data.get('discoveryVersion', 1)}  ")
    L.append(f"**Last updated:** {data.get('lastUpdated', '')}")
    L.append("")
    L.append(f"## Purpose\n{data.get('purpose') or '_unknown_'}")
    L.append("")
    L.append(f"## Page description\n{data.get('description') or '_none captured_'}")

    def section(title: str, items: list[Any], render):
        L.append("")
        L.append(f"## {title}")
        if not items:
            L.append("_none_")
            return
        for it in items:
            L.append(f"- {render(it)}")

    section("Important UI", data.get("ui", []), lambda x: str(x))
    section("Buttons", data.get("buttons", []),
            lambda b: f"{b.get('text')}" + ("  ⚠️ destructive" if b.get("danger") else ""))
    section("Forms", data.get("forms", []),
            lambda f: f"`{f.get('method', '?')}` {f.get('action') or '(inline)'} — "
                      f"{len(f.get('fields', []))} field(s)")
    section("Navigation", data.get("navigation", []),
            lambda n: f"`{n.get('route')}` {n.get('text') or ''}".strip())
    section("Redirects", data.get("redirects", []), lambda x: str(x))
    section("Common workflows", data.get("workflows", []),
            lambda w: f"**{w.get('name')}** — {' → '.join(w.get('steps', []))}"
                      + (f" (⚠️ {w['warning']})" if w.get("warning") else ""))
    section("Important warnings", data.get("warnings", []), lambda x: str(x))
    section("Dynamic behaviour", data.get("dynamic", []), lambda x: str(x))
    section("Known problems", data.get("knownProblems", []), lambda x: str(x))

    L.append("")
    L.append("## Discovery history")
    for h in data.get("history", []):
        L.append(f"- **v{h.get('version')}** ({h.get('at')}, {h.get('mode')}): {h.get('summary')}")

    L.append("")
    L.append(_DATA_MARKER)
    L.append("```json")
    L.append(json.dumps(data, indent=2))
    L.append("```")
    return "\n".join(L) + "\n"


def parseRouteMarkdown(md: str) -> dict[str, Any] | None:
    """Recover the embedded data dict from a route markdown (None if hand-written)."""
    m = _DATA_RE.search(md or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def mergeDiscovery(old: dict[str, Any] | None, new: dict[str, Any], mode: str) -> dict[str, Any]:
    """Merge a fresh discovery into the existing route skill — append, never wipe."""
    ts = utcTimestamp()
    if old is None:
        base: dict[str, Any] = {
            "route": new.get("route", "/"),
            "created": ts,
            "confidence": _DEFAULT_CONFIDENCE,
            "discoveryVersion": 0,
            "history": [],
        }
    else:
        base = dict(old)

    added: dict[str, int] = {}
    for f in _LIST_FIELDS:
        before = base.get(f, []) or []
        merged = _dedupe(list(before) + list(new.get(f, []) or []))
        if len(merged) > len(before):
            added[f] = len(merged) - len(before)
        base[f] = merged
    for f in _TEXT_FIELDS:
        if (new.get(f) or "").strip():
            base[f] = new[f]
    for f in ("imageCount", "externalLinkCount"):
        if new.get(f) is not None:
            base[f] = new[f]

    base["discoveryVersion"] = base.get("discoveryVersion", 0) + 1
    base["lastUpdated"] = ts
    base.setdefault("confidence", _DEFAULT_CONFIDENCE)
    summary = (
        "Initial discovery." if base["discoveryVersion"] == 1
        else ("Re-discovered: " + ", ".join(f"+{n} {f}" for f, n in added.items()) if added
              else "Re-discovered: no changes.")
    )
    base.setdefault("history", []).append(
        {"version": base["discoveryVersion"], "at": ts, "mode": mode, "summary": summary}
    )
    return base


class WebsiteSkillManager:
    """Per-domain operational knowledge base with O(1) JSON lookup."""

    def __init__(self, root: Path | str | None = None, mode: str | None = None) -> None:
        self.root: Path = Path(root or settings.discoveryStorage)
        self.mode: str = (mode or settings.discoveryMode or LEARN).upper()
        if self.mode not in MODES:
            self.mode = LEARN
        self._lock = threading.Lock()

    # ----- mode -------------------------------------------------------- #
    def setMode(self, mode: str) -> dict[str, Any]:
        m = (mode or "").upper()
        if m not in MODES:
            raise ValueError(f"Invalid mode '{mode}'. Use one of {MODES}.")
        self.mode = m
        return {"mode": self.mode, "modes": list(MODES)}

    def _canRead(self) -> bool:
        return self.mode in (READ_ONLY, LEARN)

    def _canWrite(self) -> bool:
        return self.mode == LEARN

    # ----- master index ------------------------------------------------ #
    @property
    def _websitesFile(self) -> Path:
        return self.root / "websites.json"

    def _loadWebsites(self) -> dict[str, Any]:
        f = self._websitesFile
        if not f.exists():
            return {"version": 1, "websites": []}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and "websites" in data else {"version": 1, "websites": []}
        except (json.JSONDecodeError, OSError):
            logger.warning("websites.json unreadable; treating as empty")
            return {"version": 1, "websites": []}

    def _saveWebsites(self, data: dict[str, Any]) -> None:
        ensureDir(self.root)
        self._websitesFile.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _websiteEntry(self, master: dict[str, Any], domain: str) -> dict[str, Any] | None:
        return next((w for w in master["websites"] if w.get("domain") == domain), None)

    # ----- per-domain index ------------------------------------------- #
    def _domainDir(self, domain: str) -> Path:
        return self.root / domain

    def _indexFile(self, domain: str) -> Path:
        return self._domainDir(domain) / "index.json"

    def _loadIndex(self, domain: str) -> dict[str, Any]:
        f = self._indexFile(domain)
        if not f.exists():
            return {"domain": domain, "routes": [], "workflows": []}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("index.json for %s unreadable; treating as empty", domain)
            return {"domain": domain, "routes": [], "workflows": []}

    def _saveIndex(self, domain: str, index: dict[str, Any]) -> None:
        ensureDir(self._domainDir(domain))
        self._indexFile(domain).write_text(json.dumps(index, indent=2), encoding="utf-8")

    # ----- lookup (the AI-context-optimized read path) ----------------- #
    def loadForUrl(self, url: str) -> dict[str, Any]:
        """websites.json -> index.json -> only the matching route markdown.

        Never loads the whole site. Returns ``{known: False}`` when nothing is
        stored or the mode forbids reading.
        """
        domain, route = domainOf(url), routeOf(url)
        if not self._canRead() or not domain:
            return {"known": False, "mode": self.mode, "domain": domain, "route": route}
        master = self._loadWebsites()
        entry = self._websiteEntry(master, domain)
        if entry is None:
            return {"known": False, "domain": domain, "route": route, "reason": "website not in index"}
        index = self._loadIndex(domain)
        routeEntry = next((r for r in index.get("routes", []) if r.get("route") == route), None)
        result: dict[str, Any] = {
            "known": True, "domain": domain, "route": route,
            "title": index.get("title"), "visitCount": entry.get("visitCount", 0),
            "workflows": [w.get("name") for w in index.get("workflows", [])],
            "knownRoutes": [r.get("route") for r in index.get("routes", [])],
        }
        if routeEntry is None:
            result["routeKnown"] = False
            return result
        skill = self._readRouteData(domain, routeEntry.get("file", ""))
        result.update({
            "routeKnown": True,
            "confidence": routeEntry.get("confidence"),
            "discoveryVersion": routeEntry.get("discoveryVersion"),
            "file": routeEntry.get("file"),
            "skill": skill,
            "needsRediscovery": (routeEntry.get("confidence", _DEFAULT_CONFIDENCE)
                                 < settings.discoveryConfidenceThreshold),
        })
        return result

    def _readRouteData(self, domain: str, relFile: str) -> dict[str, Any] | None:
        f = self._domainDir(domain) / relFile
        if not f.exists():
            return None
        return parseRouteMarkdown(f.read_text(encoding="utf-8"))

    def getWorkflow(self, domain: str, name: str) -> dict[str, Any] | None:
        index = self._loadIndex(domain)
        wf = next((w for w in index.get("workflows", []) if w.get("name") == name), None)
        if not wf:
            return None
        f = self._domainDir(domain) / wf.get("file", "")
        return {"name": name, "markdown": f.read_text(encoding="utf-8")} if f.exists() else None

    # ----- write path (LEARN only) ------------------------------------ #
    def recordVisit(self, url: str) -> None:
        """Bump visit counters in both indexes (no-op outside LEARN)."""
        if not self._canWrite():
            return
        domain = domainOf(url)
        if not domain:
            return
        ts = utcTimestamp()
        with self._lock:
            master = self._loadWebsites()
            entry = self._websiteEntry(master, domain)
            if entry is None:
                entry = {"domain": domain, "folder": domain, "index": f"{domain}/index.json",
                         "created": ts, "visitCount": 0}
                master["websites"].append(entry)
            entry["lastVisited"] = ts
            entry["visitCount"] = entry.get("visitCount", 0) + 1
            self._saveWebsites(master)

    def saveDiscovery(self, url: str, discovery: dict[str, Any]) -> dict[str, Any]:
        """Merge a fresh page discovery into the route skill + update indexes."""
        if not self._canWrite():
            raise PermissionError(f"Discovery mode is {self.mode}; cannot write skills.")
        domain, route = domainOf(url), discovery.get("route") or routeOf(url)
        if not domain:
            raise ValueError(f"Cannot derive a domain from URL: {url!r}")
        ts = utcTimestamp()
        with self._lock:
            old = self._readRouteData(domain, f"routes/{_slug(route)}.md")
            merged = mergeDiscovery(old, discovery, self.mode)
            merged["route"] = route

            relFile = f"routes/{_slug(route)}.md"
            target = self._domainDir(domain) / relFile
            ensureDir(target.parent)
            target.write_text(renderRouteMarkdown(merged), encoding="utf-8")

            # Persist any inferred workflows as their own skill files.
            wfFiles = self._writeWorkflows(domain, merged.get("workflows", []))

            index = self._loadIndex(domain)
            index["domain"] = domain
            index.setdefault("created", ts)
            index["lastUpdated"] = ts
            index.setdefault("title", merged.get("title"))
            if merged.get("route") == "/" and merged.get("title"):
                index["title"] = merged["title"]
            self._upsertRoute(index, {
                "route": route, "title": merged.get("title"), "file": relFile,
                "confidence": merged.get("confidence", _DEFAULT_CONFIDENCE),
                "discoveryVersion": merged.get("discoveryVersion", 1), "lastVisited": ts,
            })
            for name, wfFile in wfFiles:
                self._upsertWorkflow(index, {"name": name, "file": wfFile})
            self._saveIndex(domain, index)

            # Master index: ensure the domain is registered + counted.
            master = self._loadWebsites()
            entry = self._websiteEntry(master, domain)
            if entry is None:
                entry = {"domain": domain, "folder": domain, "index": f"{domain}/index.json",
                         "created": ts, "visitCount": 1}
                master["websites"].append(entry)
            entry["lastVisited"] = ts
            self._saveWebsites(master)

        return {"domain": domain, "route": route, "file": relFile,
                "discoveryVersion": merged.get("discoveryVersion"),
                "confidence": merged.get("confidence"),
                "summary": merged["history"][-1]["summary"]}

    def _writeWorkflows(self, domain: str, workflows: list[dict[str, Any]]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for wf in workflows:
            name = wf.get("name")
            if not name:
                continue
            rel = f"workflows/{_slug(name)}.md"
            target = self._domainDir(domain) / rel
            ensureDir(target.parent)
            body = [f"# {name}", "", "## Goal", name, "", "## Steps"]
            body += [f"{i + 1}. {s}" for i, s in enumerate(wf.get("steps", []))]
            if wf.get("warning"):
                body += ["", "## Known issues", f"⚠️ {wf['warning']}"]
            body += ["", "## Confidence", f"{wf.get('confidence', 80)}%", ""]
            target.write_text("\n".join(body), encoding="utf-8")
            out.append((name, rel))
        return out

    @staticmethod
    def _upsertRoute(index: dict[str, Any], routeEntry: dict[str, Any]) -> None:
        routes = index.setdefault("routes", [])
        for i, r in enumerate(routes):
            if r.get("route") == routeEntry["route"]:
                routes[i] = routeEntry
                return
        routes.append(routeEntry)

    @staticmethod
    def _upsertWorkflow(index: dict[str, Any], wfEntry: dict[str, Any]) -> None:
        wfs = index.setdefault("workflows", [])
        if not any(w.get("name") == wfEntry["name"] for w in wfs):
            wfs.append(wfEntry)

    # ----- confidence -------------------------------------------------- #
    def adjustConfidence(self, url: str, delta: int) -> dict[str, Any]:
        """Nudge a route's confidence (clamped 0-100). Drives auto-rediscovery."""
        if not self._canWrite():
            raise PermissionError(f"Discovery mode is {self.mode}; cannot write skills.")
        domain, route = domainOf(url), routeOf(url)
        with self._lock:
            index = self._loadIndex(domain)
            entry = next((r for r in index.get("routes", []) if r.get("route") == route), None)
            if entry is None:
                return {"updated": False, "reason": "route not known", "route": route}
            entry["confidence"] = max(0, min(100, entry.get("confidence", _DEFAULT_CONFIDENCE) + delta))
            # Mirror into the markdown's embedded data so the skill file stays in sync.
            data = self._readRouteData(domain, entry["file"])
            if data is not None:
                data["confidence"] = entry["confidence"]
                (self._domainDir(domain) / entry["file"]).write_text(
                    renderRouteMarkdown(data), encoding="utf-8")
            self._saveIndex(domain, index)
        return {"updated": True, "route": route, "confidence": entry["confidence"],
                "needsRediscovery": entry["confidence"] < settings.discoveryConfidenceThreshold}

    def recordSuccess(self, url: str) -> dict[str, Any]:
        return self.adjustConfidence(url, +5)

    def recordFailure(self, url: str) -> dict[str, Any]:
        return self.adjustConfidence(url, -20)

    # ----- listing / search / maintenance ----------------------------- #
    def listSkills(self, domain: str | None = None) -> dict[str, Any]:
        master = self._loadWebsites()
        if domain:
            index = self._loadIndex(domain)
            return {"domain": domain, "routes": index.get("routes", []),
                    "workflows": index.get("workflows", [])}
        return {"websites": master.get("websites", []), "count": len(master.get("websites", []))}

    def searchSkills(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Keyword search across domains/routes/titles using the JSON indexes only."""
        tokens = re.findall(r"[a-z0-9]+", (query or "").lower())
        hits: list[dict[str, Any]] = []
        for site in self._loadWebsites().get("websites", []):
            domain = site.get("domain", "")
            index = self._loadIndex(domain)
            for r in index.get("routes", []):
                hay = f"{domain} {r.get('route', '')} {r.get('title', '')}".lower()
                score = sum(hay.count(t) for t in tokens) if tokens else 1
                if score > 0:
                    hits.append({"domain": domain, "route": r.get("route"),
                                 "title": r.get("title"), "file": r.get("file"),
                                 "confidence": r.get("confidence"), "_score": score})
        hits.sort(key=lambda h: -h["_score"])
        return {"query": query, "results": hits[:limit], "matched": len(hits)}

    def exportSkills(self, domain: str | None = None) -> dict[str, Any]:
        """Serialise skills (one domain or all) into a portable dict."""
        domains = [domain] if domain else [w["domain"] for w in self._loadWebsites().get("websites", [])]
        bundle: dict[str, Any] = {"version": 1, "exportedAt": utcTimestamp(), "domains": {}}
        for d in domains:
            ddir = self._domainDir(d)
            if not ddir.exists():
                continue
            files: dict[str, str] = {}
            for f in ddir.rglob("*"):
                if f.is_file():
                    files[str(f.relative_to(ddir))] = f.read_text(encoding="utf-8")
            bundle["domains"][d] = files
        return bundle

    def importSkills(self, bundle: dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
        """Restore skills from an :meth:`exportSkills` bundle (LEARN only)."""
        if not self._canWrite():
            raise PermissionError(f"Discovery mode is {self.mode}; cannot import skills.")
        imported = 0
        with self._lock:
            master = self._loadWebsites()
            for domain, files in (bundle.get("domains") or {}).items():
                ddir = self._domainDir(domain)
                for rel, content in files.items():
                    target = ddir / rel
                    if target.exists() and not overwrite:
                        continue
                    ensureDir(target.parent)
                    target.write_text(content, encoding="utf-8")
                    imported += 1
                if self._websiteEntry(master, domain) is None:
                    master["websites"].append({
                        "domain": domain, "folder": domain, "index": f"{domain}/index.json",
                        "created": utcTimestamp(), "visitCount": 0})
            self._saveWebsites(master)
        return {"imported": imported, "domains": list((bundle.get("domains") or {}).keys())}

    def clearSkills(self, domain: str | None = None) -> dict[str, Any]:
        """Forget one domain's skills, or wipe everything."""
        if not self._canWrite():
            raise PermissionError(f"Discovery mode is {self.mode}; cannot clear skills.")
        import shutil
        with self._lock:
            if domain:
                ddir = self._domainDir(domain)
                if ddir.exists():
                    shutil.rmtree(ddir)
                master = self._loadWebsites()
                master["websites"] = [w for w in master["websites"] if w.get("domain") != domain]
                self._saveWebsites(master)
                return {"cleared": domain}
            count = len(self._loadWebsites().get("websites", []))
            if self.root.exists():
                shutil.rmtree(self.root)
            return {"cleared": "all", "domains": count}

    def status(self) -> dict[str, Any]:
        master = self._loadWebsites()
        return {
            "mode": self.mode, "modes": list(MODES), "storage": str(self.root),
            "websiteCount": len(master.get("websites", [])),
            "confidenceThreshold": settings.discoveryConfidenceThreshold,
            "autoUpdate": settings.discoveryAutoUpdate, "maxDepth": settings.discoveryMaxDepth,
        }


_skillSingleton: WebsiteSkillManager | None = None


def getWebsiteSkillManager() -> WebsiteSkillManager:
    """Return the shared :class:`WebsiteSkillManager`, creating it on first use."""
    global _skillSingleton
    if _skillSingleton is None:
        _skillSingleton = WebsiteSkillManager()
    return _skillSingleton


# --------------------------------------------------------------------------- #
# Self-check (ponytail: runnable proof merge/render/parse hold). `python -m`.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import tempfile

    assert domainOf("https://www.GitHub.com/login") == "github.com"
    assert _slug("/pull/123") == "pull_123"
    assert _slug("/") == "home"

    d1 = {"route": "/login", "title": "Sign in", "purpose": "Login page.",
          "buttons": [{"text": "Sign in"}], "workflows": [{"name": "Login", "steps": ["a", "b"]}],
          "warnings": ["Delete"], "navigation": [{"route": "/", "text": "Home"}]}
    m1 = mergeDiscovery(None, d1, LEARN)
    assert m1["discoveryVersion"] == 1 and len(m1["history"]) == 1

    d2 = {"route": "/login", "buttons": [{"text": "Sign in"}, {"text": "Google login"}]}
    m2 = mergeDiscovery(m1, d2, LEARN)
    assert m2["discoveryVersion"] == 2
    assert len(m2["buttons"]) == 2  # merged, deduped (Sign in not duplicated)
    assert m2["purpose"] == "Login page."  # carried over

    md = renderRouteMarkdown(m2)
    back = parseRouteMarkdown(md)
    assert back is not None and back["discoveryVersion"] == 2
    assert parseRouteMarkdown("# plain md, no data block") is None

    with tempfile.TemporaryDirectory() as tmp:
        mgr = WebsiteSkillManager(root=tmp, mode=LEARN)
        mgr.recordVisit("https://example.com/login")
        res = mgr.saveDiscovery("https://example.com/login", d1)
        assert res["discoveryVersion"] == 1
        loaded = mgr.loadForUrl("https://example.com/login")
        assert loaded["known"] and loaded["routeKnown"]
        assert loaded["skill"]["purpose"] == "Login page."
        assert mgr.searchSkills("login")["matched"] == 1
        assert mgr.adjustConfidence("https://example.com/login", -30)["confidence"] == 40
        bundle = mgr.exportSkills()
        assert "example.com" in bundle["domains"]
        ro = WebsiteSkillManager(root=tmp, mode=READ_ONLY)
        assert ro.loadForUrl("https://example.com/login")["known"]
        try:
            ro.saveDiscovery("https://example.com/x", d1)
            raise AssertionError("READ_ONLY must refuse writes")
        except PermissionError:
            pass
    print("website_skills self-check OK")
