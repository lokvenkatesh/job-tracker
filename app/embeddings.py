"""Local text embeddings for the pgvector matching tier. Runs on this machine (no API key, no email
text sent anywhere); the ~70 MB model downloads on first use."""

from typing import Protocol

from app.classifier.schema import Classification
from app.config import PROJECT_ROOT
from app.db.models import EMBEDDING_DIM
from app.gmail.message import EmailMessage

MODEL_NAME = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self.model_name = model_name
        self._model = None

    def embed(self, text: str) -> list[float]:
        if self._model is None:
            from fastembed import TextEmbedding  # heavy import, deferred

            self._model = TextEmbedding(
                self.model_name, cache_dir=str(PROJECT_ROOT / ".cache" / "fastembed")
            )
        vector = [float(x) for x in next(iter(self._model.embed([text])))]
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(f"expected {EMBEDDING_DIM} dims, got {len(vector)}")
        return vector


def embedding_text(msg: EmailMessage, classification: Classification) -> str:
    """What identifies an application: who, what role, and the gist. Not the full body."""
    parts = [
        classification.company or "",
        classification.role or "",
        msg.sender_name,
        msg.subject,
        msg.snippet,
    ]
    return "\n".join(p for p in parts if p)
