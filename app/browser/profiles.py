"""Named Chrome/Chromium profiles and the persisted "active profile" pointer.

The controller historically drove a single on-disk user-data directory. Real
users keep several browser profiles (one per Google/work/personal account), and
an agent driving a browser should be able to pick one — and keep using the *same*
one across chats. This module owns that:

* each named profile is a managed subdirectory under ``settings.profilesDir``,
  used as a Playwright ``launch_persistent_context`` user-data dir, so cookies
  and logins persist exactly like the original single-profile behaviour;
* the currently chosen profile is written to ``settings.activeProfileFile`` so it
  survives process restarts — the same profile reopens for the user even after a
  chat ends and a new prompt begins.

Why managed dirs rather than the system Chrome user-data dir: Chrome holds a
singleton lock on its own profile while running, so pointing Playwright at it
fails whenever the user has Chrome open. Managed dirs avoid that while still
allowing a real-Chrome *engine* via ``settings.browserChannel`` ("chrome").

Identifiers are camelCase per project convention; the on-disk JSON keys are an
external contract and stay snake-free, simple strings.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from app.utils.config import settings
from app.utils.helpers import ensureDir, utcTimestamp
from app.utils.logger import getLogger

logger = getLogger("browser.profiles")

# A safe default profile name used when the caller asks for "random" but no
# profiles exist yet, or when legacy single-profile behaviour is wanted.
DEFAULT_PROFILE = "default"

_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def sanitizeProfileName(name: str) -> str:
    """Reduce *name* to a filesystem-safe slug (letters, digits, ``_``, ``-``)."""
    slug = _NAME_RE.sub("-", (name or "").strip()).strip("-")
    return slug or DEFAULT_PROFILE


class ProfileManager:
    """Create, list, resolve and remember named browser profiles."""

    def __init__(self, profilesDir: Path | None = None, activeFile: Path | None = None) -> None:
        self.profilesDir: Path = Path(profilesDir or settings.profilesDir)
        self.activeFile: Path = Path(activeFile or settings.activeProfileFile)

    # ------------------------------------------------------------------ #
    # Filesystem layout
    # ------------------------------------------------------------------ #
    def resolveDir(self, name: str) -> Path:
        """Return the user-data directory for profile *name* (not created)."""
        return self.profilesDir / sanitizeProfileName(name)

    def createProfile(self, name: str) -> dict[str, Any]:
        """Create (or no-op if present) a named profile directory."""
        safe = sanitizeProfileName(name)
        path = self.resolveDir(safe)
        existed = path.exists()
        ensureDir(path)
        logger.info("Profile %s %s at %s", safe, "exists" if existed else "created", path)
        return {"name": safe, "path": str(path), "created": not existed}

    def listProfiles(self) -> list[dict[str, Any]]:
        """List known profiles with basic metadata (active flag, last-used time)."""
        ensureDir(self.profilesDir)
        active = self.getActiveProfile()
        profiles: list[dict[str, Any]] = []
        for entry in sorted(self.profilesDir.iterdir()):
            if not entry.is_dir():
                continue
            profiles.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "active": entry.name == active,
                    "hasData": any(entry.iterdir()),
                }
            )
        return profiles

    def profileNames(self) -> list[str]:
        """Return just the names of existing profiles."""
        return [p["name"] for p in self.listProfiles()]

    # ------------------------------------------------------------------ #
    # Active-profile pointer (persisted across process restarts)
    # ------------------------------------------------------------------ #
    def getActiveProfile(self) -> str | None:
        """Return the persisted active profile name, or ``None`` if unset."""
        try:
            raw = json.loads(self.activeFile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        name = raw.get("active") if isinstance(raw, dict) else None
        return name if isinstance(name, str) and name else None

    def setActiveProfile(self, name: str) -> dict[str, Any]:
        """Persist *name* as the active profile (creating its dir if needed)."""
        info = self.createProfile(name)
        ensureDir(self.activeFile.parent)
        payload = {"active": info["name"], "updatedAt": utcTimestamp()}
        self.activeFile.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Active profile set to %s", info["name"])
        return {"active": info["name"], "path": info["path"]}

    def clearActiveProfile(self) -> None:
        """Forget the active profile (next open will prompt for selection)."""
        try:
            self.activeFile.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Selection helpers
    # ------------------------------------------------------------------ #
    def chooseRandom(self) -> str:
        """Pick a random existing profile, creating ``default`` if none exist."""
        names = self.profileNames()
        if not names:
            self.createProfile(DEFAULT_PROFILE)
            return DEFAULT_PROFILE
        return random.choice(names)

    def resolveActiveDir(self) -> Path | None:
        """Return the user-data dir for the active profile, or ``None`` if unset."""
        name = self.getActiveProfile()
        return self.resolveDir(name) if name else None


# --------------------------------------------------------------------- #
# Process-wide singleton accessor (mirrors getBrowserManager's pattern)
# --------------------------------------------------------------------- #
_profileManagerSingleton: ProfileManager | None = None


def getProfileManager() -> ProfileManager:
    """Return the shared :class:`ProfileManager`, creating it on first use."""
    global _profileManagerSingleton
    if _profileManagerSingleton is None:
        _profileManagerSingleton = ProfileManager()
    return _profileManagerSingleton
