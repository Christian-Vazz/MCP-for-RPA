"""Base de conhecimento por usuário: RAG (trechos de arquivos/sites) + memória de longo prazo.

Desempenho: a matriz de vetores do usuário fica em memória do processo e só é recarregada quando
a "versão" (contador no cache) muda. A busca é uma multiplicação matriz×vetor (numpy).
"""
import hashlib
import logging
import re
import threading

import numpy as np
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.conf import cfg
from apps.errors import UserFacingError

from .embeddings import get_embedder
from .models import Chunk, Document, Memory

log = logging.getLogger(__name__)
_lock = threading.Lock()
_MATS: dict[int, tuple] = {}
_MEMS: dict[int, tuple] = {}


# ----------------------------- versão (invalidação de cache) -----------------------------
def version(user) -> int:
    return cache.get_or_set(f"kbv:{user.pk}", 1, None)


def bump_version(user):
    key = f"kbv:{user.pk}"
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 2, None)


# ----------------------------- chunking -----------------------------
def chunk_text(text: str, size: int = 900, overlap: int = 150, max_chunks: int = 400):
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > size:
            if cur:
                chunks.append(cur)
                cur = ""
            i = 0
            while i < len(para):
                chunks.append(para[i:i + size])
                i += size - overlap
            continue
        if len(cur) + len(para) + 2 <= size:
            cur = f"{cur}\n\n{para}" if cur else para
        else:
            chunks.append(cur)
            cur = para
    if cur:
        chunks.append(cur)
    return chunks[:max_chunks]


# ----------------------------- RAG -----------------------------
def ingest(user, source_type: str, source: str, title: str, text: str) -> dict:
    text = (text or "").strip()[: cfg("MAX_INGEST_CHARS")]
    if len(text) < 40:
        return {"status": "skipped", "chunks": 0}
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    emb = get_embedder()
    with transaction.atomic():
        doc = Document.objects.filter(user=user, source_type=source_type, source=source).first()
        if doc and doc.content_hash == content_hash:
            return {"status": "unchanged", "chunks": doc.chunk_count}
        pieces = chunk_text(text)
        existing = Chunk.objects.filter(user=user).count() - (doc.chunk_count if doc else 0)
        if existing + len(pieces) > cfg("MAX_CHUNKS"):
            raise UserFacingError("Base de conhecimento cheia. Remova fontes antigas (rag_forget).")
        vectors = emb.embed(pieces)
        if doc:
            doc.chunks.all().delete()
        else:
            doc = Document(user=user, source_type=source_type, source=source)
        doc.title, doc.content_hash, doc.chunk_count = (title or source)[:200], content_hash, len(pieces)
        doc.save()
        Chunk.objects.bulk_create([
            Chunk(document=doc, user=user, idx=i, text=p, embedding=vectors[i].tobytes())
            for i, p in enumerate(pieces)
        ])
    bump_version(user)
    return {"status": "indexed", "chunks": len(pieces)}


def maybe_learn(user, source_type, source, title, text):
    """Ingere conteúdo usado pelo agente SOMENTE se o usuário consentiu. Nunca quebra a ferramenta."""
    try:
        from apps.accounts.models import get_profile
        if get_profile(user).allow_learning:
            ingest(user, source_type, source, title, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("maybe_learn falhou: %s", exc)


def forget_source(user, source: str) -> int:
    deleted, _ = Document.objects.filter(user=user, source=source).delete()
    if deleted:
        bump_version(user)
    return deleted


def _matrix(user):
    ver = version(user)
    with _lock:
        cached = _MATS.get(user.pk)
        if cached and cached[0] == ver:
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
    with _lock:
        _MATS[user.pk] = (ver, mat, meta)
    return mat, meta


def search(user, query: str, k: int = 4, min_score: float | None = None):
    mat, meta = _matrix(user)
    if mat is None or not query.strip():
        return []
    min_score = cfg("RAG_MIN_SCORE") if min_score is None else min_score
    q = get_embedder().embed([query])[0]
    scores = mat @ q
    order = np.argsort(-scores)[: max(1, k)]
    return [{**meta[i], "score": round(float(scores[i]), 3)} for i in order if scores[i] >= min_score]


# ----------------------------- Memória -----------------------------
def _memory_matrix(user):
    ver = version(user)
    with _lock:
        cached = _MEMS.get(user.pk)
        if cached and cached[0] == ver:
            return cached[1], cached[2]
    dim = get_embedder().dim
    rows = [m for m in Memory.objects.filter(user=user) if len(m.embedding) == dim * 4]
    mat = (np.frombuffer(b"".join(bytes(m.embedding) for m in rows), dtype=np.float32).reshape(-1, dim)
           if rows else None)
    with _lock:
        _MEMS[user.pk] = (ver, mat, rows)
    return mat, rows


def save_memory(user, content: str, kind: str = "fact", importance: int = 3) -> dict:
    content = (content or "").strip()
    if len(content) < 3:
        raise UserFacingError("Memória vazia.")
    if len(content) > 500:
        raise UserFacingError("Memória longa demais (máx. 500 caracteres). Resuma.")
    kind = kind if kind in dict(Memory.KINDS) else "fact"
    importance = max(1, min(5, int(importance)))
    vec = get_embedder().embed([content])[0]
    mat, rows = _memory_matrix(user)
    if mat is not None:  # quase duplicada? atualiza em vez de criar
        scores = mat @ vec
        best = int(np.argmax(scores))
        if scores[best] >= 0.92:
            mem = rows[best]
            mem.content, mem.importance, mem.embedding = content, max(mem.importance, importance), vec.tobytes()
            mem.save(update_fields=["content", "importance", "embedding"])
            bump_version(user)
            return {"id": mem.pk, "status": "updated"}
    if Memory.objects.filter(user=user).count() >= cfg("MAX_MEMORIES"):
        victim = Memory.objects.filter(user=user).order_by("importance", "last_used_at", "created_at").first()
        if victim:
            victim.delete()
    mem = Memory.objects.create(user=user, kind=kind, content=content, importance=importance,
                                embedding=vec.tobytes())
    bump_version(user)
    return {"id": mem.pk, "status": "created"}


def recall(user, query: str, k: int = 5, min_score: float = 0.15):
    mat, rows = _memory_matrix(user)
    if mat is None or not query.strip():
        return []
    scores = mat @ get_embedder().embed([query])[0]
    # prioriza relevância; importância desempata levemente
    ranked = sorted(range(len(rows)), key=lambda i: -(scores[i] + 0.03 * rows[i].importance))
    out = []
    for i in ranked[:k]:
        if scores[i] >= min_score:
            m = rows[i]
            out.append({"id": m.pk, "kind": m.kind, "content": m.content, "score": round(float(scores[i]), 3)})
    if out:
        Memory.objects.filter(pk__in=[o["id"] for o in out]).update(last_used_at=timezone.now())
    return out


def forget_memory(user, memory_id: int) -> bool:
    deleted, _ = Memory.objects.filter(user=user, pk=memory_id).delete()
    if deleted:
        bump_version(user)
    return bool(deleted)


# ----------------------------- Estatísticas (dashboard) -----------------------------
def stats(user) -> dict:
    emb = get_embedder()
    docs = Document.objects.filter(user=user)
    chunks = Chunk.objects.filter(user=user).count()
    memories = Memory.objects.filter(user=user).count()
    by_type = list(docs.values("source_type").annotate(n=Count("id"), chunks=Sum("chunk_count")))
    return {
        "documents": docs.count(),
        "chunks": chunks,
        "memories": memories,
        "dimension": emb.dim,
        "backend": emb.name,
        "vector_bytes": (chunks + memories) * emb.dim * 4,
        "by_type": [{"type": r["source_type"], "documents": r["n"], "chunks": r["chunks"] or 0} for r in by_type],
        "recent_sources": [{"type": d.source_type, "source": d.source, "chunks": d.chunk_count}
                           for d in docs[:5]],
    }
