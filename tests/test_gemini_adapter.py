"""Tests for the Gemini provider adapter.

These run fully OFFLINE — they never import the ``google-genai`` SDK and never
hit the network. They verify that:

* the adapter builds a Gemini function declaration for every canonical tool,
* its tool surface stays in lock-step with the Claude/OpenAI adapters (the
  shared registry is the single source of truth),
* no declaration has the empty-``properties`` schema Gemini rejects,
* the JSON-schema -> Gemini-schema translation upper-cases ``type`` while
  preserving ``enum``/``description``/``required``,
* ``runConversation`` fails with a clear, actionable error when the SDK or the
  API key is missing (rather than crashing obscurely).
"""

from __future__ import annotations

import pytest

from app.agents.claude_adapter import TOOL_SPECS, ClaudeAdapter
from app.agents.gemini_adapter import DEFAULT_GEMINI_MODEL, GeminiAdapter
from app.agents.openai_adapter import OpenAIAdapter


def test_default_model() -> None:
    assert GeminiAdapter().model == DEFAULT_GEMINI_MODEL == "gemini-2.5-flash"
    assert GeminiAdapter("gemini-2.5-pro").model == "gemini-2.5-pro"


def test_build_tools_covers_every_spec() -> None:
    """One declaration per canonical tool, names preserved exactly."""
    declarations = GeminiAdapter().buildTools()
    assert len(declarations) == len(TOOL_SPECS)
    names = [d["name"] for d in declarations]
    assert names == [s["name"] for s in TOOL_SPECS]
    # The Aether-only tools must be gone from the registry entirely.
    for removed in ("select_backend", "open_tab_with_profile", "ask_user"):
        assert removed not in names


def test_tool_surface_matches_other_adapters() -> None:
    """Gemini drives the SAME tools as Claude/OpenAI (shared registry)."""
    gemini = {d["name"] for d in GeminiAdapter().buildTools()}
    claude = {t["name"] for t in ClaudeAdapter().buildTools()}
    openai = {t["function"]["name"] for t in OpenAIAdapter().buildTools()}
    assert gemini == claude == openai


def test_no_empty_parameters() -> None:
    """Gemini rejects a declaration whose parameters have empty properties.

    A no-argument tool must omit ``parameters`` entirely; a tool with arguments
    must carry a non-empty ``properties`` map.
    """
    for declaration in GeminiAdapter().buildTools():
        params = declaration.get("parameters")
        if params is not None:
            assert params.get("properties"), declaration["name"]


def test_no_arg_tool_has_no_parameters() -> None:
    decls = {d["name"]: d for d in GeminiAdapter().buildTools()}
    # close_browser / status take no arguments.
    assert "parameters" not in decls["close_browser"]
    assert "parameters" not in decls["status"]
    # navigate takes a url -> it must carry a schema.
    assert "parameters" in decls["navigate"]


def test_schema_translation_uppercases_types_and_keeps_constraints() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "a name", "enum": ["a", "b"]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
    }
    out = GeminiAdapter._toGeminiSchema(schema)
    assert out["type"] == "OBJECT"
    assert out["properties"]["name"]["type"] == "STRING"
    assert out["properties"]["name"]["enum"] == ["a", "b"]
    assert out["properties"]["name"]["description"] == "a name"
    assert out["properties"]["tags"]["type"] == "ARRAY"
    assert out["properties"]["tags"]["items"]["type"] == "STRING"
    assert out["required"] == ["name"]


@pytest.mark.asyncio
async def test_run_conversation_requires_sdk_or_key(monkeypatch) -> None:
    """Without the SDK or a key, the loop raises a clear RuntimeError."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        await GeminiAdapter().runConversation("do nothing", maxTurns=1)
    message = str(excinfo.value)
    assert "google-genai" in message or "GEMINI_API_KEY" in message
