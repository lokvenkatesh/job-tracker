"""Shared Claude request options."""

# Server-side refusal fallback: if the model declines, the API re-runs the request on a fallback
# model in the same call. Only some models accept it (Haiku 5.5 doesn't).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)


def fallback_kwargs(model: str) -> dict:
    if model in FALLBACK_MODELS:
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}
