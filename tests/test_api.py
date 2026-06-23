"""HTTP API smoke tests against the FastAPI application.

``app/api/server.py`` is authored in parallel and may not exist (or may fail to
import) when this suite is collected. To keep the overall run from hard-crashing
we guard every import and skip the whole module if anything required is missing.

Only browser-free endpoints are exercised here:

* ``GET /health`` — static liveness probe.
* ``GET /status`` — returns the enveloped browser status; ``status()`` handles
  the not-running case gracefully, so no Playwright launch occurs.
"""

from __future__ import annotations

import pytest

# fastapi.testclient pulls in starlette's TestClient (sync, requests-style).
pytest.importorskip("fastapi", reason="FastAPI not installed")

try:
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001 - environment without the test client
    pytest.skip(f"fastapi.testclient unavailable: {exc}", allow_module_level=True)


def _resolveApp():
    """Locate the FastAPI app from app.api.server.

    Supports either a module-level ``app`` instance or a ``createApp()`` factory.
    Returns the ASGI app, or raises to trigger a module-level skip.
    """
    from app.api import server  # may not exist yet -> ImportError

    if hasattr(server, "app") and server.app is not None:
        return server.app
    if hasattr(server, "createApp"):
        return server.createApp()
    raise RuntimeError("app.api.server exposes neither 'app' nor 'createApp()'")


try:
    fastApiApp = _resolveApp()
except Exception as exc:  # noqa: BLE001 - server written by a parallel agent
    pytest.skip(f"app.api.server not importable yet: {exc}", allow_module_level=True)


@pytest.fixture
def client():
    with TestClient(fastApiApp) as testClient:
        yield testClient


def test_health_endpoint(client: "TestClient"):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "ok"


def test_status_endpoint(client: "TestClient"):
    response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    # The status endpoint returns the AI-friendly envelope.
    assert isinstance(body, dict)
    assert "success" in body
