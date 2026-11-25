"""``GET /v1/models`` — advertise the Antigravity models this server exposes.

We list OpenAI-friendly slugs (handy as a client ``default_model``) plus the
exact display names the `agy` CLI accepts, so a client can pick either; the
actual model used per request is whatever the client sends, resolved through
``config.resolve_model``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import KNOWN_MODELS
from app.openai_models import ModelCard, ModelList

router = APIRouter()

# Slugs first, then the CLI's canonical display names.
_SLUGS = [
    "gemini-4-neon",
    "gemini-4-neon-low",
    "gemini-4-neon-medium",
    "gemini-4-neon-high",
    "gemini-4-neon-xhigh",
    "gemini-4-carbon",
    "gemini-4-carbon-low",
    "gemini-4-carbon-medium",
    "gemini-4-carbon-high",
    "gemini-4-argon",
    "gemini-4-argon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-4-hydrogen",
    "gemini-4-hydrogen-low",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-3.8-flash",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-high",
    "claude-opus-5.5",
    "claude-sonnet-5.5",
    "gpt-oss-120b",
    "orchestrator-neon",
    "orchestrator-carbon",
    "orchestrator-argon",
]

_MODEL_IDS = _SLUGS + KNOWN_MODELS


@router.get("/v1/models", response_model=ModelList)
async def list_models() -> ModelList:
    return ModelList(data=[ModelCard(id=mid) for mid in _MODEL_IDS])


@router.get("/v1/models/{model_id}", response_model=ModelCard)
async def get_model(model_id: str) -> ModelCard:
    return ModelCard(id=model_id)
