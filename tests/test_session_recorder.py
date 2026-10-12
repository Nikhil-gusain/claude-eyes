"""Unit tests for the structured session recorder (browser-free).

These cover the pure bookkeeping: recording only while active, pausing during
replay, and the save -> load round-trip preserving steps.
"""

from __future__ import annotations

from pathlib import Path

from app.browser.session_recorder import SessionRecorder


def _recorder(tmp_path: Path) -> SessionRecorder:
    return SessionRecorder(outputDir=tmp_path)


def test_recordsOnlyWhileRecording(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    # Not started yet -> ignored.
    rec.record("click", {"selector": "#a"})
    assert rec.steps == []

    rec.start("demo")
    rec.record("click", {"selector": "#a"})
    rec.record("fill", {"selector": "#b", "value": "hi"})
    assert len(rec.steps) == 2
    assert rec.steps[0]["action"] == "click"
    assert "timestamp" in rec.steps[0] and "offsetMs" in rec.steps[0]

    rec.stop()
    rec.record("click", {"selector": "#c"})  # stopped -> ignored
    assert len(rec.steps) == 2


def test_pauseSuppressesRecording(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.start("demo")
    rec.paused = True
    rec.record("click", {"selector": "#a"})
    assert rec.steps == []
    rec.paused = False
    rec.record("click", {"selector": "#a"})
    assert len(rec.steps) == 1


def test_noneParamsAreStripped(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.start("demo")
    rec.record("click", {"selector": "#a", "timeoutMs": None, "humanize": None})
    assert rec.steps[0]["params"] == {"selector": "#a"}


def test_saveLoadRoundTrip(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.start("trip")
    rec.record("navigate", {"url": "https://example.com"})
    rec.record("click", {"selector": "#go"})
    saved = rec.save()
    assert Path(saved["path"]).exists()
    assert saved["stepCount"] == 2

    fresh = SessionRecorder(outputDir=tmp_path)
    loaded = fresh.load(saved["path"])
    assert loaded["stepCount"] == 2
    assert fresh.steps[0]["action"] == "navigate"
    assert fresh.recording is False


def test_loadMissingFileRaises(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    try:
        rec.load(str(tmp_path / "absent.json"))
    except FileNotFoundError:
        return
    raise AssertionError("Expected FileNotFoundError for a missing session file")
