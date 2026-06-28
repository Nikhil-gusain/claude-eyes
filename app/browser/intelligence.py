"""AI-judgment helpers — the "smart" tools that call Claude.

These turn raw browser automation into *self-correcting* automation:

* :func:`verifyGoal` — look at a screenshot and judge whether a natural-language
  goal is met (``{success, confidence, reason}``).
* :func:`findElement` — pick the best element for a description like
  "blue login button" from a list of on-page candidates.
* :func:`planActions` — break a high-level goal into an ordered action plan.

Anthropic is an OPTIONAL dependency here, exactly like MarkItDown/Tesseract
elsewhere: when the ``anthropic`` SDK or ``ANTHROPIC_API_KEY`` is missing every
call returns a structured error envelope (never raises), so the import is always
safe and the feature degrades cleanly. Model id comes from ``settings.aiModel``.
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


def aiAvailable() -> bool:
    """Whether the Claude-backed tools can run (SDK installed + API key set)."""
    try:
        import anthropic  # noqa: F401, PLC0415 - optional dependency
    except Exception:  # noqa: BLE001
        return False
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _unavailable() -> dict[str, Any]:
    try:
        import anthropic  # noqa: F401, PLC0415

        havePkg = True
    except Exception:  # noqa: BLE001
        havePkg = False
    if not havePkg:
        details = "Install the 'anthropic' package (pip install anthropic)."
    elif not os.getenv("ANTHROPIC_API_KEY"):
        details = "Set ANTHROPIC_API_KEY in the environment."
    else:  # pragma: no cover - defensive
        details = "Claude is not configured."
    return {"error": "AI features are unavailable", "details": details, "aiAvailable": False}


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


def _complete(content: list[dict[str, Any]], system: str, maxTokens: int = 1024) -> str:
    """Run one non-streaming Claude message and return its concatenated text."""
    import anthropic  # noqa: PLC0415 - optional dependency

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=settings.aiModel,
        max_tokens=maxTokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def verifyGoal(goal: str, imageBytes: bytes, mediaType: str = "image/png") -> dict[str, Any]:
    """Judge whether *goal* is satisfied in the screenshot *imageBytes*."""
    if not aiAvailable():
        return _unavailable()
    system = (
        "You verify whether a stated goal is visually satisfied in a webpage "
        "screenshot. Reply with ONLY a JSON object: "
        '{"success": bool, "confidence": number 0..1, "reason": short string}.'
    )
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": mediaType,
                       "data": base64.b64encode(imageBytes).decode("ascii")},
        },
        {"type": "text", "text": f"Goal: {goal}\nIs this goal met? Reply with the JSON only."},
    ]
    try:
        data = _extractJson(_complete(content, system))
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
        data = _extractJson(_complete([{"type": "text", "text": prompt}], system))
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
        steps = _extractJson(_complete([{"type": "text", "text": prompt}], system, maxTokens=2048))
    except Exception as exc:  # noqa: BLE001
        return {"error": "Planning failed", "details": f"{type(exc).__name__}: {exc}",
                "aiAvailable": True}
    if not isinstance(steps, list):
        steps = [steps]
    return {"goal": goal, "plan": steps, "stepCount": len(steps), "aiAvailable": True}
