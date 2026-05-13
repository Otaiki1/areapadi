from __future__ import annotations
import os


async def generate_embedding(text: str) -> list[float] | None:
    """
    Generate a text embedding using OpenAI text-embedding-3-small.
    Returns None when the API key is absent or the call fails.
    The seller service handles None gracefully — items fall back to text-only search.
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        response = await client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],
        )
        return response.data[0].embedding
    except Exception:
        return None
