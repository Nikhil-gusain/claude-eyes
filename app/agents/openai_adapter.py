"""OpenAI provider adapter for the AI Browser Controller.

This adapter reuses the canonical, provider-neutral tool registry
(:data:`app.agents.claude_adapter.TOOL_SPECS`) and the shared async
:func:`app.agents.claude_adapter.dispatchTool` so the browser tool surface stays
defined in exactly one place (DRY). It transforms those specs into OpenAI
function-calling tool schema and runs the OpenAI tool-calling loop via the
``openai`` Python SDK.

The default model is ``gpt-4o`` and the ``OPENAI_API_KEY`` environment variable
supplies credentials.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.agents.claude_adapter import TOOL_SPECS, dispatchTool
from app.browser import intelligence
from app.utils.logger import getLogger

logger = getLogger("agents.openai")

# Default model for this adapter.
DEFAULT_OPENAI_MODEL = "gpt-4o"


class OpenAIAdapter:
    """Adapter that drives an OpenAI function-calling loop.

    The adapter transforms the canonical :data:`TOOL_SPECS` into OpenAI tool
    definitions and runs a chat-completions tool-calling loop, dispatching each
    requested tool through the shared :func:`dispatchTool`.
    """

    def __init__(self, model: str = DEFAULT_OPENAI_MODEL) -> None:
        """Construct the adapter.

        Args:
            model: The OpenAI model id to use. Defaults to ``gpt-4o``.
        """
        self.model: str = model
        self.apiKey: str | None = os.getenv("OPENAI_API_KEY")

    def buildTools(self) -> list[dict[str, Any]]:
        """Build OpenAI function-calling tool definitions from the registry.

        Returns:
            A list of dicts shaped as
            ``{"type": "function", "function": {"name", "description", "parameters"}}``
            for the full tool set.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
            for spec in TOOL_SPECS
        ]

    async def dispatchTool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call to the shared :func:`dispatchTool`.

        Args:
            name: The snake_case tool name.
            arguments: The parsed tool input.

        Returns:
            The envelope dict from the ``BrowserManager``.
        """
        return await dispatchTool(name, arguments)

    async def runConversation(self, userPrompt: str, maxTurns: int = 10) -> dict[str, Any]:
        """Run the OpenAI tool-calling loop for a single user prompt.

        Lazily imports the ``openai`` SDK so the module imports even when the SDK
        is not installed. Raises a clear :class:`RuntimeError` if the SDK or the
        API key is missing.

        Args:
            userPrompt: The user's natural-language instruction.
            maxTurns: Maximum number of assistant turns before stopping.

        Returns:
            A summary dict ``{"finalText", "turns", "toolCalls"}``.
        """
        try:
            import openai  # noqa: PLC0415 - lazy import by design
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "The 'openai' package is required to run an OpenAI conversation. "
                "Install it with: pip install openai"
            ) from exc

        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Export your OpenAI API key before "
                "calling runConversation()."
            )

        # Judgment tools (verify_goal / find_element / plan_actions) this run
        # invokes should reason with OpenAI too — match the driver.
        intelligence.setAiProvider("openai")

        # Reads OPENAI_API_KEY from the environment.
        client = openai.OpenAI()

        tools = self.buildTools()
        messages: list[dict[str, Any]] = [{"role": "user", "content": userPrompt}]
        toolCalls: list[dict[str, Any]] = []
        finalTextParts: list[str] = []
        turns = 0

        for _ in range(maxTurns):
            turns += 1
            response = client.chat.completions.create(
                model=self.model,
                tools=tools,
                messages=messages,
            )
            message = response.choices[0].message

            # Preserve the assistant message (text and/or tool_calls) verbatim.
            messages.append(message.model_dump(exclude_none=True))

            if message.content:
                finalTextParts.append(message.content)

            if message.tool_calls:
                for call in message.tool_calls:
                    rawArguments = call.function.arguments or "{}"
                    try:
                        arguments = json.loads(rawArguments)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Failed to parse tool arguments for '%s': %s",
                            call.function.name,
                            rawArguments,
                        )
                        arguments = {}
                    logger.info("OpenAI requested tool '%s'", call.function.name)
                    result = await self.dispatchTool(call.function.name, arguments)
                    toolCalls.append(
                        {"name": call.function.name, "input": arguments, "result": result}
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result),
                        }
                    )
                continue

            # No tool calls requested — the assistant is done.
            break

        return {
            "finalText": "".join(finalTextParts),
            "turns": turns,
            "toolCalls": toolCalls,
        }


async def main() -> None:
    """Usage example: construct the adapter and print its OpenAI tool schema.

    The API is intentionally NOT called here, since no key may be present in the
    environment. To exercise a real conversation, set ``OPENAI_API_KEY`` and call
    ``await adapter.runConversation(...)``.
    """
    adapter = OpenAIAdapter()
    tools = adapter.buildTools()
    print(json.dumps(tools, indent=2))
    print(f"\nBuilt {len(tools)} OpenAI tool definitions for model '{adapter.model}'.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
