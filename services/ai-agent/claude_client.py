"""
LLM client — uses Google Gemini via its OpenAI-compatible endpoint.
Keeps the same call_claude_json interface so nothing else needs to change.
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from openai import AsyncOpenAI
from shared.logger import get_logger

logger = get_logger("claude-client")

HAIKU = "gemini-2.0-flash"
SONNET = "gemini-2.0-flash"

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.getenv("GEMINI_API_KEY"),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    return _client


def _strip_fence(text: str) -> str:
    """Remove markdown code fences the model sometimes wraps JSON in."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
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
    Call Gemini with a system prompt and expect a JSON object back.
    history is a list of {role, content} dicts for prior turns (last 10 max).
    Returns {} on any failure — callers must handle the empty-dict case.
    """
    client = get_client()

    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_message})

    try:
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        return json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        logger.error("llm_json_parse_error", error=str(exc))
        return {}
    except Exception as exc:
        logger.error("llm_call_failed", model=model, error=str(exc))
        return {}
