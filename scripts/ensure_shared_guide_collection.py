"""Create or validate the configured shared-guide Qdrant collection safely."""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.rag.qdrant_index import (
    QdrantSharedGuideIndex,
    create_qdrant_client,
    validate_collection_name,
)


_V1_DIMENSION = 768


def _timeout(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("QDRANT_TIMEOUT_SECONDS must be positive")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("QDRANT_TIMEOUT_SECONDS must be positive")
    return parsed


def provision_collection(
    *,
    settings_obj: Any = settings,
    client_factory: Callable[..., Any] = create_qdrant_client,
    index_factory: Callable[..., Any] = QdrantSharedGuideIndex,
) -> str:
    collection = validate_collection_name(settings_obj.QDRANT_COLLECTION)
    dimension = settings_obj.EMBEDDING_DIMENSION
    if isinstance(dimension, bool) or dimension != _V1_DIMENSION:
        raise ValueError("shared-guide V1 requires EMBEDDING_DIMENSION=768")
    url = str(settings_obj.QDRANT_URL or "").strip()
    if not url:
        raise ValueError("QDRANT_URL is required")
    client = client_factory(
        url=url,
        api_key=settings_obj.QDRANT_API_KEY,
        timeout_seconds=_timeout(settings_obj.QDRANT_TIMEOUT_SECONDS),
    )
    index = index_factory(
        client=client,
        collection=collection,
        dimension=_V1_DIMENSION,
    )
    index.ensure_collection()
    return collection


def main() -> int:
    try:
        collection = provision_collection()
    except Exception as error:
        print(
            f"qdrant_collection status=failed error_class={type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(
        f"qdrant_collection status=ready collection={collection} "
        "dimension=768 distance=Cosine"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
