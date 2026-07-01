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

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

from app.agents.claude_adapter import SYSTEM_PROMPT, TOOL_SPECS, dispatchTool
from app.agents.gemini_keys import loadKeys, withRotation
from app.agents.narrate import narrateAction, narrateResult
from app.browser import intelligence
from app.utils.logger import getLogger

logger = getLogger("agents.gemini")

# Default fallback chain for this adapter. Flash is fast + cheap and supports
# function calling; pro is stronger for harder multi-step automation. When a
# model returns 503 (overloaded / "high demand") the adapter advances to the
# next one in this list. Override the whole chain with the ``GEMINI_MODELS`` env
# var (comma/space-separated), or pin a single model with ``--model``.
DEFAULT_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]

# Kept for backward compatibility (imports / tests): the head of the chain.
DEFAULT_GEMINI_MODEL = DEFAULT_GEMINI_MODELS[0]


def _isOverloaded(exc: BaseException) -> bool:
    """True for transient server-side unavailability (503), matched on text.

    Matches on text rather than SDK types to avoid coupling, mirroring the
    rate-limit check in :mod:`app.agents.gemini_keys`. These are *not* quota
    errors — they mean the model itself is temporarily overloaded, so the right
    response is to switch models rather than rotate keys.
    """
    text = f"{getattr(exc, 'code', '')} {getattr(exc, 'status', '')} {exc}".upper()
    return any(m in text for m in ("503", "UNAVAILABLE", "OVERLOADED", "HIGH DEMAND"))


class GeminiAdapter:
    """Adapter that drives a manual agentic function-calling loop with Gemini.

    The adapter transforms the canonical :data:`TOOL_SPECS` into Gemini function
    declarations and runs the conversation loop against the ``google-genai``
    async client, dispatching each requested tool through the shared
    :func:`dispatchTool`.
    """

    def __init__(self, model: str | None = None) -> None:
        """Construct the adapter.

        Args:
            model: Gemini model id to prefer. When given it heads the fallback
                chain; the rest of the chain (``GEMINI_MODELS`` env, then the
                built-in defaults) still follows so a 503 on the pinned model
                can fall through. Defaults to ``gemini-2.5-flash`` at the head.
        """
        # Ordered, deduped fallback chain. ``self.model`` is the current/head
        # model (kept for backward compatibility and logging).
        self.models: list[str] = self._resolveModels(model)
        self._modelIndex: int = 0
        self.model: str = self.models[0]
        # The SDK reads GOOGLE_API_KEY natively; we also accept GEMINI_API_KEY.
        self.apiKey: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    @staticmethod
    def _resolveModels(explicit: str | None) -> list[str]:
        """Build the ordered, deduped model fallback chain.

        Precedence: an explicit ``--model`` first, then the ``GEMINI_MODELS`` env
        var (comma/space-separated), then the built-in :data:`DEFAULT_GEMINI_MODELS`.
        """
        models: list[str] = []

        def add(name: str | None) -> None:
            name = (name or "").strip()
            if name and name not in models:
                models.append(name)

        add(explicit)
        for token in os.getenv("GEMINI_MODELS", "").replace(",", " ").split():
            add(token)
        for name in DEFAULT_GEMINI_MODELS:
            add(name)
        return models

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

    async def _generate(self, genai: Any, contents: list[Any], config: Any) -> Any:
        """One model turn, with key rotation and 503 model fallback.

        Tries the current model first (rotating API keys past 429s via
        :func:`withRotation`). If the model is overloaded (503), it advances
        through the remaining models in the chain and *sticks* on the first one
        that responds, so the rest of the run stays on the working model rather
        than retrying the overloaded one every turn.

        A new ``genai.Client`` is built per attempt — construction is cheap (it
        just holds the key + transport config) and lets a rotated key take effect.
        """
        last: BaseException | None = None
        for index in range(self._modelIndex, len(self.models)):
            model = self.models[index]
            try:
                response = await withRotation(
                    lambda key: genai.Client(api_key=key).aio.models.generate_content(
                        model=model,
                        contents=contents,
                        config=config,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - re-raised unless overloaded
                if not _isOverloaded(exc):
                    raise
                last = exc
                nextModel = self.models[index + 1] if index + 1 < len(self.models) else None
                logger.warning(
                    "Gemini model '%s' unavailable (503)%s",
                    model,
                    f" — falling back to '{nextModel}'" if nextModel else " — no more models",
                )
                continue
            if index != self._modelIndex:
                logger.info("Gemini now using model '%s'", model)
            self._modelIndex = index  # stick to the working model
            self.model = model
            return response
        raise RuntimeError(
            f"All Gemini models are unavailable (503): {self.models[self._modelIndex:]}"
        ) from last

    async def runConversation(
        self,
        userPrompt: str,
        maxTurns: int = 12,
        onEvent: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        feedback: "asyncio.Queue[str] | None" = None,
    ) -> dict[str, Any]:
        """Run the agentic function-calling loop for a single user prompt.

        Lazily imports the ``google-genai`` SDK so the module imports even when
        the SDK is not installed. Raises a clear :class:`RuntimeError` if the SDK
        or the API key is missing.

        Args:
            userPrompt: The user's natural-language instruction.
            maxTurns: Maximum number of model turns before stopping.
            onEvent: Optional async sink for live progress events (the "thinking
                window"). Receives dicts like ``{"type": "action", "text": ...}``
                with types ``thought`` / ``action`` / ``result`` / ``feedback`` /
                ``done`` / ``error``. Left ``None`` for the CLI — zero behaviour
                change.
            feedback: Optional queue of user corrections. Drained between model
                turns and injected into the conversation as ``[User correction]``
                messages, so the user can steer mid-task. Left ``None`` for the CLI.

        Returns:
            A summary dict ``{"finalText", "turns", "toolCalls"}``.
        """

        async def emit(event: dict[str, Any]) -> None:
            """Best-effort push to the event sink; never let UI errors break the run."""
            if onEvent is None:
                return
            try:
                await onEvent(event)
            except Exception:  # noqa: BLE001 - the UI must never crash the agent
                logger.debug("onEvent sink raised; continuing", exc_info=True)

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

        try:
            for _ in range(maxTurns):
                turns += 1

                # Between-steps interruption: fold any queued user corrections into
                # the conversation before the model's next turn so it can react.
                while feedback is not None and not feedback.empty():
                    note = feedback.get_nowait()
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=f"[User correction] {note}")],
                        )
                    )
                    await emit({"type": "feedback", "text": note})

                response = await self._generate(genai, contents, config)

                candidate = response.candidates[0] if response.candidates else None
                if candidate is None or candidate.content is None:
                    break

                # Preserve the full model turn (text + function_call parts).
                contents.append(candidate.content)

                # Collect any text the model emitted this turn.
                for part in candidate.content.parts or []:
                    if getattr(part, "text", None):
                        finalTextParts.append(part.text)
                        await emit({"type": "thought", "text": part.text})

                calls = response.function_calls or []
                if not calls:
                    # No tool calls requested — the model is done.
                    break

                responseParts = []
                for call in calls:
                    arguments = dict(call.args or {})
                    logger.info("Gemini requested tool '%s'", call.name)
                    await emit({"type": "action", "text": narrateAction(call.name, arguments)})
                    result = await self.dispatchTool(call.name, arguments)
                    await emit({"type": "result", "text": narrateResult(call.name, result)})
                    toolCalls.append({"name": call.name, "input": arguments, "result": result})
                    # Gemini's function_response value must be a dict.
                    payload = result if isinstance(result, dict) else {"result": result}
                    responseParts.append(
                        types.Part.from_function_response(name=call.name, response=payload)
                    )
                contents.append(types.Content(role="user", parts=responseParts))
        except Exception as exc:  # noqa: BLE001 - surface to the UI, then re-raise
            await emit({"type": "error", "text": str(exc)})
            raise

        finalText = "".join(finalTextParts)
        await emit({"type": "done", "text": finalText, "turns": turns})
        return {
            "finalText": finalText,
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
