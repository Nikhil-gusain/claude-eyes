"""Google Gemini provider adapter for the AI Browser Controller.

This adapter makes the browser **Gemini-controlled**: Gemini becomes the in-process
"brain" that drives the same Playwright-backed Chrome browser the rest of the app
exposes — analogous to the :class:`~app.agents.claude_adapter.ClaudeAdapter` and
:class:`~app.agents.openai_adapter.OpenAIAdapter`, just backed by the Google
``google-genai`` SDK.

It reuses the canonical, provider-neutral tool registry
(:data:`app.agents.claude_adapter.TOOL_SPECS`), the shared async
:func:`app.agents.claude_adapter.dispatchTool`, and the shared
:data:`app.agents.claude_adapter.SYSTEM_PROMPT`, so the browser tool surface stays
defined in exactly one place (DRY). It transforms those specs into Gemini
*function declarations* and runs Gemini's function-calling loop.

The default model is ``gemini-2.5-flash``; the ``GEMINI_API_KEY`` (or the SDK's
native ``GOOGLE_API_KEY``) environment variable supplies credentials. The
``google-genai`` SDK is an OPTIONAL dependency: the module always imports, and a
clear :class:`RuntimeError` is raised only if you actually try to run a
conversation without the SDK or a key installed.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.agents.claude_adapter import SYSTEM_PROMPT, TOOL_SPECS, dispatchTool
from app.agents.gemini_keys import loadKeys, withRotation
from app.browser import intelligence
from app.utils.logger import getLogger

logger = getLogger("agents.gemini")

# Default model for this adapter. Flash is fast + cheap and supports function
# calling; pass model="gemini-2.5-pro" for harder, multi-step automation.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class GeminiAdapter:
    """Adapter that drives a manual agentic function-calling loop with Gemini.

    The adapter transforms the canonical :data:`TOOL_SPECS` into Gemini function
    declarations and runs the conversation loop against the ``google-genai``
    async client, dispatching each requested tool through the shared
    :func:`dispatchTool`.
    """

    def __init__(self, model: str = DEFAULT_GEMINI_MODEL) -> None:
        """Construct the adapter.

        Args:
            model: The Gemini model id to use. Defaults to ``gemini-2.5-flash``.
        """
        self.model: str = model
        # The SDK reads GOOGLE_API_KEY natively; we also accept GEMINI_API_KEY.
        self.apiKey: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    # ------------------------------------------------------------------ #
    # Schema translation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _toGeminiSchema(schema: dict[str, Any]) -> dict[str, Any]:
        """Convert a JSON-schema ``parameters`` object to Gemini's schema dialect.

        Gemini accepts an OpenAPI-subset schema whose ``type`` values are
        UPPERCASE enum names (``STRING``, ``OBJECT``, ...). This recurses through
        ``properties``/``items`` upper-casing ``type`` while preserving
        ``description``, ``enum`` and ``required``.
        """
        if not isinstance(schema, dict):
            return schema
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                out["type"] = value.upper()
            elif key == "properties" and isinstance(value, dict):
                out["properties"] = {
                    propName: GeminiAdapter._toGeminiSchema(propSchema)
                    for propName, propSchema in value.items()
                }
            elif key == "items":
                out["items"] = GeminiAdapter._toGeminiSchema(value)
            else:
                out[key] = value
        return out

    def buildTools(self) -> list[dict[str, Any]]:
        """Build Gemini function declarations from the canonical registry.

        Returns a list of ``{"name", "description", ["parameters"]}`` dicts. Note
        that Gemini REJECTS a function declaration whose ``parameters`` have an
        empty ``properties`` map, so tools that take no arguments are emitted with
        no ``parameters`` key at all. This method needs no SDK, so it is safe to
        call (and unit-test) without ``google-genai`` installed.
        """
        declarations: list[dict[str, Any]] = []
        for spec in TOOL_SPECS:
            declaration: dict[str, Any] = {
                "name": spec["name"],
                "description": spec["description"],
            }
            params = spec.get("parameters") or {}
            properties = params.get("properties") or {}
            # Only attach a schema when there is at least one property — an empty
            # parameters object is invalid for Gemini.
            if properties:
                declaration["parameters"] = self._toGeminiSchema(params)
            declarations.append(declaration)
        return declarations

    async def dispatchTool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call to the shared :func:`dispatchTool`.

        Args:
            name: The snake_case tool name.
            arguments: The parsed tool input.

        Returns:
            The envelope dict from the ``BrowserManager``.
        """
        return await dispatchTool(name, arguments)

    async def runConversation(self, userPrompt: str, maxTurns: int = 12) -> dict[str, Any]:
        """Run the agentic function-calling loop for a single user prompt.

        Lazily imports the ``google-genai`` SDK so the module imports even when
        the SDK is not installed. Raises a clear :class:`RuntimeError` if the SDK
        or the API key is missing.

        Args:
            userPrompt: The user's natural-language instruction.
            maxTurns: Maximum number of model turns before stopping.

        Returns:
            A summary dict ``{"finalText", "turns", "toolCalls"}``.
        """
        try:
            from google import genai  # noqa: PLC0415 - lazy import by design
            from google.genai import types  # noqa: PLC0415 - lazy import by design
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "The 'google-genai' package is required to run a Gemini "
                "conversation. Install it with: pip install google-genai"
            ) from exc

        if not loadKeys():
            raise RuntimeError(
                "No Gemini API key set. Export GEMINI_API_KEY or GEMINI_API_KEYS "
                "(comma/space-separated to rotate past free-tier limits) before "
                "calling runConversation(). GOOGLE_API_KEY is also accepted."
            )

        # Judgment tools (verify_goal / find_element / plan_actions) this run
        # invokes should reason with Gemini too — match the driver.
        intelligence.setAiProvider("gemini")

        # Build the tool surface and a config that uses MANUAL function calling
        # (we dispatch each call ourselves to the async BrowserManager).
        tools = [types.Tool(function_declarations=self.buildTools())]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=tools,
            temperature=0.0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        contents: list[Any] = [
            types.Content(role="user", parts=[types.Part(text=userPrompt)])
        ]
        toolCalls: list[dict[str, Any]] = []
        finalTextParts: list[str] = []
        turns = 0

        for _ in range(maxTurns):
            turns += 1
            # New client per call so a rate-limited key can rotate mid-run; Client
            # construction is cheap (it just holds the key + transport config).
            response = await withRotation(
                lambda key: genai.Client(api_key=key).aio.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
            )

            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None:
                break

            # Preserve the full model turn (text + function_call parts).
            contents.append(candidate.content)

            # Collect any text the model emitted this turn.
            for part in candidate.content.parts or []:
                if getattr(part, "text", None):
                    finalTextParts.append(part.text)

            calls = response.function_calls or []
            if not calls:
                # No tool calls requested — the model is done.
                break

            responseParts = []
            for call in calls:
                arguments = dict(call.args or {})
                logger.info("Gemini requested tool '%s'", call.name)
                result = await self.dispatchTool(call.name, arguments)
                toolCalls.append({"name": call.name, "input": arguments, "result": result})
                # Gemini's function_response value must be a dict.
                payload = result if isinstance(result, dict) else {"result": result}
                responseParts.append(
                    types.Part.from_function_response(name=call.name, response=payload)
                )
            contents.append(types.Content(role="user", parts=responseParts))

        return {
            "finalText": "".join(finalTextParts),
            "turns": turns,
            "toolCalls": toolCalls,
        }


async def main() -> None:
    """Usage example: construct the adapter and print its Gemini tool schema.

    The API is intentionally NOT called here, since no key may be present in the
    environment. To exercise a real conversation, set ``GEMINI_API_KEY`` and call
    ``await adapter.runConversation(...)``.
    """
    adapter = GeminiAdapter()
    tools = adapter.buildTools()
    print(json.dumps(tools, indent=2))
    print(f"\nBuilt {len(tools)} Gemini function declarations for model '{adapter.model}'.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
