"""Unit tests for ProfileManager (browser-free).

Covers creating/listing profiles and — the key requirement — that the active
profile choice is persisted to disk so it survives a fresh manager instance
(i.e. the same profile reopens for the user in a later chat / process).
"""

from __future__ import annotations

from pathlib import Path

from app.browser.profiles import DEFAULT_PROFILE, ProfileManager, sanitizeProfileName


def _manager(tmp_path: Path) -> ProfileManager:
    return ProfileManager(
        profilesDir=tmp_path / "profiles",
        activeFile=tmp_path / "active_profile.json",
    )


def test_sanitizeProfileName() -> None:
    assert sanitizeProfileName("My Work Account!") == "My-Work-Account"
    assert sanitizeProfileName("   ") == DEFAULT_PROFILE
    assert sanitizeProfileName("a/b\\c") == "a-b-c"


def test_createAndListProfiles(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    first = mgr.createProfile("alice")
    assert first["created"] is True
    # Creating again is a no-op (already exists).
    assert mgr.createProfile("alice")["created"] is False

    mgr.createProfile("bob")
    names = mgr.profileNames()
    assert names == ["alice", "bob"]


def test_activeProfilePersistsAcrossInstances(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    assert mgr.getActiveProfile() is None  # nothing chosen yet -> prompt territory

    mgr.setActiveProfile("work")
    assert mgr.getActiveProfile() == "work"

    # A brand-new manager (simulating a new process / later chat) reads the same
    # persisted choice from disk.
    reopened = _manager(tmp_path)
    assert reopened.getActiveProfile() == "work"
    assert reopened.resolveActiveDir() == (tmp_path / "profiles" / "work")


def test_chooseRandomCreatesDefaultWhenEmpty(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    chosen = mgr.chooseRandom()
    assert chosen == DEFAULT_PROFILE
    assert chosen in mgr.profileNames()


def test_clearActiveProfile(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.setActiveProfile("x")
    mgr.clearActiveProfile()
    assert mgr.getActiveProfile() is None
