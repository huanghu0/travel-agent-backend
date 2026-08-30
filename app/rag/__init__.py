"""Domain models and deterministic text construction for guide retrieval."""

from .models import BuiltRetrievalText, RagReference
from .text_builder import EmbeddingTextBuilder

__all__ = ["BuiltRetrievalText", "EmbeddingTextBuilder", "RagReference"]
