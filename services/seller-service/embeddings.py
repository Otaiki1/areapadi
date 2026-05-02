from __future__ import annotations
import os
from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    return _client


async def generate_embedding(text: str) -> list[float]:
    """Generate a 1536-dim embedding using text-embedding-3-small."""
    client = get_openai_client()
    response = await client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],
    )
    return response.data[0].embedding
