"""Armazenamento vetorial dos trechos do RAG. Dois motores com a mesma interface:

* LocalVectorStore: matriz numpy em memória do processo (padrão, offline).
* PineconeVectorStore: índice serverless do Pinecone, com UM namespace por usuário (isolamento).

O SQLite continua sendo a fonte da verdade (texto + vetor de cada trecho). Isso permite reindexar,
trocar de motor a qualquer momento e cair para a busca local se o Pinecone estiver fora do ar.
"""
import logging
import threading

import numpy as np

from apps.conf import cfg
from apps.errors import UserFacingError

from .embeddings import get_embedder
from .models import Chunk, Document

log = logging.getLogger(__name__)
META_TEXT_CHARS = 2000  # o Pinecone limita metadados a 40 KB por vetor; o trecho tem ~900 caracteres


def store_name() -> str:
    name = cfg("VECTOR_STORE")
    if name == "auto":
        return "pinecone" if cfg("PINECONE_API_KEY") else "local"
    return name


class LocalVectorStore:
    name = "local"

    def __init__(self):
        self._lock = threading.Lock()
        self._mats: dict[int, tuple] = {}

    def upsert(self, user, doc, chunks, vectors):
        """Nada a fazer: os vetores já estão no SQLite e a matriz recarrega quando a versão muda."""

    def delete(self, user, ranges):
        pass

    def _matrix(self, user, ver):
        with self._lock:
            cached = self._mats.get(user.pk)
            if cached and cached[0] == ver and (cached[1] is None or cached[1].shape[1] == get_embedder().dim):
                return cached[1], cached[2]
        dim = get_embedder().dim
        rows = [r for r in Chunk.objects.filter(user=user).values_list(
            "embedding", "text", "document__source", "document__title", "document__source_type")
            if len(r[0]) == dim * 4]
        if rows:
            mat = np.frombuffer(b"".join(bytes(r[0]) for r in rows), dtype=np.float32).reshape(-1, dim)
            meta = [{"text": r[1], "source": r[2], "title": r[3], "type": r[4]} for r in rows]
        else:
            mat, meta = None, []
        with self._lock:
            self._mats[user.pk] = (ver, mat, meta)
        return mat, meta

    def search(self, user, qvec, k, ver):
        mat, meta = self._matrix(user, ver)
        if mat is None:
            return []
        scores = mat @ qvec
        order = np.argsort(-scores)[: max(1, k)]
        return [{**meta[i], "score": round(float(scores[i]), 3)} for i in order]


class PineconeVectorStore:
    name = "pinecone"

    def __init__(self, client=None):
        if client is None:
            from pinecone import Pinecone  # import tardio: dependência só é exigida quando o Pinecone é usado
            client = Pinecone(api_key=cfg("PINECONE_API_KEY"))
        self.pc = client
        self._index = None
        self._lock = threading.Lock()

    @staticmethod
    def namespace(user) -> str:
        return f"{cfg('PINECONE_NAMESPACE_PREFIX')}u{user.pk}"

    @staticmethod
    def vector_id(doc_id: int, idx: int) -> str:
        return f"{doc_id}:{idx}"

    def index(self):
        with self._lock:
            if self._index is None:
                name, dim = cfg("PINECONE_INDEX"), get_embedder().dim
                if not self.pc.has_index(name):
                    from pinecone import ServerlessSpec
                    self.pc.create_index(name=name, dimension=dim, metric="cosine",
                                         spec=ServerlessSpec(cloud=cfg("PINECONE_CLOUD"), region=cfg("PINECONE_REGION")))
                else:
                    current = getattr(self.pc.describe_index(name), "dimension", None)
                    if current and current != dim:
                        raise UserFacingError(
                            f"O índice Pinecone '{name}' tem dimensão {current}, mas os embeddings atuais têm {dim}. "
                            "Use outro PINECONE_INDEX ou apague o índice e rode reindex_knowledge.")
                self._index = self.pc.Index(name=name)
            return self._index

    def upsert(self, user, doc, chunks, vectors):
        items = [{"id": self.vector_id(doc.pk, c.idx), "values": vectors[i].tolist(),
                  "metadata": {"text": c.text[:META_TEXT_CHARS], "source": doc.source, "title": doc.title,
                               "type": doc.source_type, "doc_id": doc.pk, "user_id": user.pk}}
                 for i, c in enumerate(chunks) if np.any(vectors[i])]  # vetor nulo é rejeitado pelo Pinecone
        index, ns = self.index(), self.namespace(user)
        for i in range(0, len(items), 100):
            index.upsert(vectors=items[i:i + 100], namespace=ns)

    def delete(self, user, ranges):
        """ranges: [(doc_id, primeiro_trecho, fim_exclusivo)]. Apaga por id: o serverless não apaga por filtro."""
        ids = [self.vector_id(doc_id, i) for doc_id, start, stop in ranges for i in range(start, stop)]
        index, ns = self.index(), self.namespace(user)
        for i in range(0, len(ids), 1000):
            index.delete(ids=ids[i:i + 1000], namespace=ns)

    def stats(self, user) -> dict:
        """Contagem real no Pinecone (eventualmente consistente: um upsert recente pode levar alguns segundos)."""
        res = self.index().describe_index_stats()
        summary = (getattr(res, "namespaces", None) or {}).get(self.namespace(user))
        count = getattr(summary, "vector_count", None)
        if count is None and isinstance(summary, dict):
            count = summary.get("vector_count", 0)
        return {"index": cfg("PINECONE_INDEX"), "namespace": self.namespace(user), "remote_vectors": int(count or 0),
                "index_total": int(getattr(res, "total_vector_count", 0) or 0)}

    def search(self, user, qvec, k, ver):
        if not np.any(qvec):
            return []
        res = self.index().query(vector=qvec.tolist(), top_k=max(1, k), namespace=self.namespace(user),
                                 include_metadata=True)
        hits = [(m.metadata or {}, float(m.score)) for m in (res.matches or [])]
        # defesa extra: só devolve trechos de documentos que ainda existem e são DESTE usuário
        alive = set(Document.objects.filter(user=user, pk__in=[md.get("doc_id") for md, _ in hits])
                    .values_list("pk", flat=True))
        return [{"text": md.get("text", ""), "source": md.get("source", ""), "title": md.get("title", ""),
                 "type": md.get("type", ""), "score": round(score, 3)}
                for md, score in hits if md.get("doc_id") in alive]


_lock = threading.Lock()
_stores: dict[str, object] = {}


def get_store(name: str | None = None):
    name = name or store_name()
    with _lock:
        if name not in _stores:
            _stores[name] = PineconeVectorStore() if name == "pinecone" else LocalVectorStore()
        return _stores[name]


def set_store(name: str, instance):
    """Usado nos testes para injetar um Pinecone falso."""
    with _lock:
        if instance is None:
            _stores.pop(name, None)
        else:
            _stores[name] = instance
