"""AI-judgment helpers — the "smart" tools, backed by a pluggable LLM provider.

These turn raw browser automation into *self-correcting* automation:

* :func:`verifyGoal` — look at a screenshot and judge whether a natural-language
  goal is met (``{success, confidence, reason}``).
* :func:`findElement` — pick the best element for a description like
  "blue login button" from a list of on-page candidates.
* :func:`planActions` — break a high-level goal into an ordered action plan.

The provider is whoever is DRIVING the browser: the in-process agent adapters
(Gemini / Claude / OpenAI) call :func:`setAiProvider` so judgment uses the same
brain; the MCP path (Claude Code) keeps ``settings.aiProvider`` (default
``claude``). Every provider SDK is an OPTIONAL dependency — when the active
provider's SDK or API key is missing, each call returns a structured error
envelope (never raises), so the import is always safe and the feature degrades
cleanly.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

from app.utils.config import settings
from app.utils.logger import getLogger

logger = getLogger("browser.intelligence")


# --------------------------------------------------------------------------- #
# Active provider — the brain doing the judgment. Defaults to the configured
# provider; the in-process adapters override it to match whoever is driving.
# --------------------------------------------------------------------------- #
_activeProvider: str = settings.aiProvider


def setAiProvider(name: str) -> None:
    """Set which provider backs the smart tools (``claude``/``gemini``/``openai``).

    Called by the agent adapters so that, e.g., a Gemini-driven run also judges
    goals and finds elements with Gemini.
    """
    global _activeProvider
    _activeProvider = (name or "").lower() or settings.aiProvider


def getAiProvider() -> str:
    """Return the currently active judgment provider."""
    return _activeProvider


# Per-provider readiness: (importable SDK?, env key present?, how to fix).
def _providerStatus(provider: str) -> tuple[bool, str]:
    """Return ``(ready, details)`` for *provider* without importing on success path."""
    if provider == "gemini":
        try:
            from google import genai  # noqa: F401, PLC0415 - optional dependency
        except Exception:  # noqa: BLE001
            return False, "Install the 'google-genai' package (pip install google-genai)."
        from app.agents.gemini_keys import loadKeys  # noqa: PLC0415 - avoid cycle

        if not loadKeys():
            return False, "Set GEMINI_API_KEY / GEMINI_API_KEYS (or GOOGLE_API_KEY)."
        return True, ""
    if provider == "openai":
        try:
            import openai  # noqa: F401, PLC0415 - optional dependency
        except Exception:  # noqa: BLE001
            return False, "Install the 'openai' package (pip install openai)."
        if not os.getenv("OPENAI_API_KEY"):
            return False, "Set OPENAI_API_KEY in the environment."
        return True, ""
    # Default: claude.
    try:
        import anthropic  # noqa: F401, PLC0415 - optional dependency
    except Exception:  # noqa: BLE001
        return False, "Install the 'anthropic' package (pip install anthropic)."
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False, "Set ANTHROPIC_API_KEY in the environment."
    return True, ""


def aiAvailable() -> bool:
    """Whether the ACTIVE provider's smart tools can run (SDK installed + key set)."""
    return _providerStatus(_activeProvider)[0]


def _unavailable() -> dict[str, Any]:
    ready, details = _providerStatus(_activeProvider)
    if ready:  # pragma: no cover - defensive
        details = f"The '{_activeProvider}' provider is not configured."
    return {
        "error": "AI features are unavailable",
        "details": details,
        "provider": _activeProvider,
        "aiAvailable": False,
    }


def _extractJson(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply (tolerates fences)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"No JSON found in model reply: {text[:200]}")


def _complete(
    system: str,
    userText: str,
    *,
    imageBytes: bytes | None = None,
    mediaType: str = "image/png",
    maxTokens: int = 1024,
) -> str:
    """Run one non-streaming completion on the ACTIVE provider; return its text.

    Provider-neutral surface: a system prompt, the user text, and an optional
    screenshot. Each provider builds its own request shape internally so the
    three smart tools above stay backend-agnostic.
    """
    provider = _activeProvider
    if provider == "gemini":
        return _completeGemini(system, userText, imageBytes, mediaType, maxTokens)
    if provider == "openai":
        return _completeOpenAI(system, userText, imageBytes, mediaType, maxTokens)
    return _completeClaude(system, userText, imageBytes, mediaType, maxTokens)


def _completeClaude(
    system: str, userText: str, imageBytes: bytes | None, mediaType: str, maxTokens: int
) -> str:
    """One Claude (Anthropic) message; vision when *imageBytes* is given."""
    import anthropic  # noqa: PLC0415 - optional dependency

    content: list[dict[str, Any]] = []
    if imageBytes is not None:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mediaType,
                    "data": base64.b64encode(imageBytes).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": userText})
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=settings.aiModel,
        max_tokens=maxTokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def _completeGemini(
    system: str, userText: str, imageBytes: bytes | None, mediaType: str, maxTokens: int
) -> str:
    """One Gemini (google-genai) generate_content; vision when *imageBytes* is given.

    Asks for ``application/json`` so the smart tools get a clean JSON reply.
    """
    from google import genai  # noqa: PLC0415 - optional dependency
    from google.genai import types  # noqa: PLC0415 - optional dependency

    from app.agents.gemini_keys import withRotationSync  # noqa: PLC0415 - avoid cycle

    parts: list[Any] = [types.Part(text=userText)]
    if imageBytes is not None:
        parts.append(types.Part.from_bytes(data=imageBytes, mime_type=mediaType))

    def _call(apiKey: str) -> str:
        client = genai.Client(api_key=apiKey)
        response = client.models.generate_content(
            model=settings.geminiAiModel,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.0,
                max_output_tokens=maxTokens,
                response_mime_type="application/json",
            ),
        )
        return response.text or ""

    return withRotationSync(_call)


def _completeOpenAI(
    system: str, userText: str, imageBytes: bytes | None, mediaType: str, maxTokens: int
) -> str:
    """One OpenAI chat completion; vision (data-URI image) when *imageBytes* is given."""
    import openai  # noqa: PLC0415 - optional dependency

    userContent: list[dict[str, Any]] = [{"type": "text", "text": userText}]
    if imageBytes is not None:
        b64 = base64.b64encode(imageBytes).decode("ascii")
        userContent.append(
            {"type": "image_url", "image_url": {"url": f"data:{mediaType};base64,{b64}"}}
        )
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=settings.openaiAiModel,
        max_tokens=maxTokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": userContent},
        ],
    )
    return response.choices[0].message.content or ""


def verifyGoal(goal: str, imageBytes: bytes, mediaType: str = "image/png") -> dict[str, Any]:
    """Judge whether *goal* is satisfied in the screenshot *imageBytes*."""
    if not aiAvailable():
        return _unavailable()
    system = (
        "You verify whether a stated goal is visually satisfied in a webpage "
        "screenshot. Reply with ONLY a JSON object: "
        '{"success": bool, "confidence": number 0..1, "reason": short string}.'
    )
    try:
        data = _extractJson(
            _complete(
                system,
                f"Goal: {goal}\nIs this goal met? Reply with the JSON only.",
                imageBytes=imageBytes,
                mediaType=mediaType,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": "Goal verification failed", "details": f"{type(exc).__name__}: {exc}",
                "aiAvailable": True}
    return {
        "success": bool(data.get("success")),
        "confidence": data.get("confidence"),
        "reason": data.get("reason"),
        "goal": goal,
        "aiAvailable": True,
    }


def findElement(description: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose the candidate element best matching *description*.

    *candidates* are ``{index, selector, tag, text, role, ariaLabel, ...}`` dicts
    produced from the page. Returns the chosen ``selector`` plus confidence/reason.
    """
    if not aiAvailable():
        return _unavailable()
    if not candidates:
        return {"error": "No candidate elements", "details": "The page exposed no interactive elements.",
                "aiAvailable": True}
    system = (
        "You match a natural-language element description to the single best "
        "candidate from a JSON list. Reply with ONLY a JSON object: "
        '{"index": int, "selector": string, "confidence": number 0..1, '
        '"reason": short string}. Use the candidate\'s own selector verbatim. '
        'If nothing matches, use index -1.'
    )
    prompt = (
        f"Description: {description}\n\nCandidates (JSON):\n"
        f"{json.dumps(candidates, ensure_ascii=False)}\n\nReply with the JSON only."
    )
    try:
        data = _extractJson(_complete(system, prompt))
    except Exception as exc:  # noqa: BLE001
        return {"error": "Element finding failed", "details": f"{type(exc).__name__}: {exc}",
                "aiAvailable": True}
    index = data.get("index", -1)
    selector = data.get("selector")
    # Prefer the candidate's real selector when the model echoed a valid index.
    if isinstance(index, int) and 0 <= index < len(candidates):
        selector = candidates[index].get("selector", selector)
    found = bool(selector) and index != -1
    return {
        "found": found,
        "selector": selector if found else None,
        "index": index,
        "confidence": data.get("confidence"),
        "reason": data.get("reason"),
        "description": description,
        "aiAvailable": True,
    }


def planActions(goal: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Break *goal* into an ordered list of high-level browser actions."""
    if not aiAvailable():
        return _unavailable()
    system = (
        "You plan browser automation. Given a goal and optional page context, "
        "output ONLY a JSON array of steps, each "
        '{"action": one of [navigate, click, fill, scroll, wait, extract, '
        'screenshot, verify], "target": string|null, "value": string|null, '
        '"why": short string}. Keep it minimal and ordered.'
    )
    prompt = f"Goal: {goal}\n"
    if context:
        prompt += f"Page context (JSON):\n{json.dumps(context, ensure_ascii=False)[:4000]}\n"
    prompt += "Reply with the JSON array only."
    try:
        steps = _extractJson(_complete(system, prompt, maxTokens=2048))
    except Exception as exc:  # noqa: BLE001
        return {"error": "Planning failed", "details": f"{type(exc).__name__}: {exc}",
                "aiAvailable": True}
    if not isinstance(steps, list):
        steps = [steps]
    return {"goal": goal, "plan": steps, "stepCount": len(steps), "aiAvailable": True}
