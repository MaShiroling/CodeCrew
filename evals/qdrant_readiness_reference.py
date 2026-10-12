"""Eval-only repeated-read control for later paired experiments.

Reproduces the old 5-GET recall shape using current payload/contract validation;
not a byte-identical historical deployment. No app configuration selects this.
"""

from app.chat.qdrant_vector_sync import QdrantVersionedVectorBackend
from app.chat.vector_contracts import VectorBackendError, VectorSearch
from app.chat.vector_sync_contracts import VectorMutation


class RepeatedReadinessBackend(QdrantVersionedVectorBackend):
    def _require_sync_ready(self):
        self._require_ready()
        schema = self._collection()
        if schema.get("sync_revision", {}).get("data_type") != "integer":
            raise VectorBackendError("Qdrant synchronization revision index incompatible")

    def _require_search_ready(self):
        self._require_ready()  # Historical base-search guard, after versioned entrance guard.

    def search_current(self, request, mutations):
        if (not isinstance(request, VectorSearch) or request.model != self.model or not isinstance(mutations, tuple)
                or len(mutations) != len(request.candidates)
                or any(not isinstance(item, VectorMutation) or item.model != self.model
                       or item.record is None or item.record.anchor != anchor
                       for item, anchor in zip(mutations, request.candidates, strict=True))):
            raise ValueError("current search requires matching active canonical mutations")
        if request.candidates:
            self._require_sync_ready()
        return self._search(request, expected_payloads={
            self.mutation_point_id(item): self.mutation_payload(item) for item in mutations})
