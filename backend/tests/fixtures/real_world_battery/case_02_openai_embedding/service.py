"""Same two-level-chain scanner limitation as case 01, a different
resource: openai.Embedding.create, also removed in openai>=1.0."""

import openai


def embed(text: str) -> list[float]:
    resp = openai.Embedding.create(input=text, model="text-embedding-ada-002")
    return resp["data"][0]["embedding"]
