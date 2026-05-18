import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

CHROMA_HOST = os.getenv("CHROMA_HOST", "chromadb")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))


class ChromaMemory:
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import chromadb
            self._client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
            log.info("ChromaDB connected at %s:%s", CHROMA_HOST, CHROMA_PORT)
        except Exception as e:
            log.warning("ChromaDB unavailable: %s", e)
        return self._client

    def _get_collection(self, name: str):
        client = self._get_client()
        if not client:
            return None
        try:
            return client.get_or_create_collection(name)
        except Exception as e:
            log.warning("ChromaDB collection error: %s", e)
            return None

    async def store(self, collection: str, doc_id: str, text: str, metadata: dict = None):
        col = self._get_collection(collection)
        if not col:
            return
        try:
            col.upsert(
                ids=[doc_id],
                documents=[text],
                metadatas=[metadata or {}],
            )
        except Exception as e:
            log.warning("ChromaDB store error: %s", e)

    async def query(self, collection: str, query: str, n_results: int = 5) -> list:
        col = self._get_collection(collection)
        if not col:
            return []
        try:
            results = col.query(query_texts=[query], n_results=n_results)
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            return [{"text": d, "metadata": m} for d, m in zip(docs, metas)]
        except Exception as e:
            log.warning("ChromaDB query error: %s", e)
            return []

    async def delete(self, collection: str, doc_id: str):
        col = self._get_collection(collection)
        if col:
            try:
                col.delete(ids=[doc_id])
            except Exception as e:
                log.warning("ChromaDB delete error: %s", e)
