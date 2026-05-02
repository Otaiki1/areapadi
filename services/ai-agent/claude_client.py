"""
Thin wrapper around the Anthropic SDK.
All stage prompts are sent with cache_control so the static system text is
cached across the 5-minute prompt-caching TTL, cutting costs on high-volume
conversations.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from anthropic import AsyncAnthropic
from shared.logger import get_logger

logger = get_logger("claude-client")

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-5"

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def _strip_fence(text: str) -> str:
    """Remove markdown code fences that Claude sometimes wraps JSON in."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (```json or ```) and last line (```)
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


async def call_claude_json(
    system: str,
    user_message: str,
    model: str = HAIKU,
    max_tokens: int = 512,
    history: list[dict] | None = None,
) -> dict:
    """
    Call Claude with a cached system prompt and expect a JSON object back.
    history is a list of {role, content} dicts for prior turns (last 10 max).
    Returns {} on any failure — callers must handle the empty-dict case.
    """
    client = get_client()
    messages: list[dict] = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_message})

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        raw = response.content[0].text
        return json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        logger.error("claude_json_parse_error", error=str(exc))
        return {}
    except Exception as exc:
        logger.error("claude_call_failed", model=model, error=str(exc))
        return {}
