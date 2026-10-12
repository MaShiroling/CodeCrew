"""Side-effect-free storage/index construction; remote preparation is explicit."""

from app.chat.qdrant_vector_backend import QdrantEndpoint, QdrantMemoryVectorBackend
from app.chat.sqlite_vector_backend import SQLiteMemoryVectorBackend
from app.chat.vector_contracts import MemoryVectorBackend, VectorModel
from app.chat.vector_memory import MemoryVectorIndex
from app.config import Settings
from app.storage import SQLiteDatabase


def build_memory_vector_backend(settings: Settings, database: SQLiteDatabase,
                                model: VectorModel) -> MemoryVectorBackend:
    # Construction is side-effect-free; Qdrant initialization is explicit.
    if settings.memory_vector_store == "sqlite":
        return SQLiteMemoryVectorBackend(database)
    return QdrantMemoryVectorBackend(QdrantEndpoint(
        base_url=settings.memory_qdrant_url, collection_prefix=settings.memory_qdrant_collection_prefix,
        api_key=settings.memory_qdrant_api_key.get_secret_value(),
        timeout_seconds=settings.memory_qdrant_timeout_seconds,
    ), model)


def build_memory_vector_index(settings, database, embedder):
    if settings.memory_vector_store == "sqlite":
        return MemoryVectorIndex(database, embedder, min_similarity=settings.memory_embedding_min_similarity)
    from app.chat.qdrant_memory_index import QdrantMemoryVectorIndex
    return QdrantMemoryVectorIndex(database, embedder, QdrantEndpoint(
        base_url=settings.memory_qdrant_url, collection_prefix=settings.memory_qdrant_collection_prefix,
        api_key=settings.memory_qdrant_api_key.get_secret_value(),
        timeout_seconds=settings.memory_qdrant_timeout_seconds,
    ), min_similarity=settings.memory_embedding_min_similarity)
