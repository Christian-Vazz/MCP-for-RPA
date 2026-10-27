"""Base de conhecimento por usuário: RAG (trechos de arquivos, sites e textos) + memória de longo prazo.

"Memória" tem duas formas: fatos curtos (Memory, injetados quando relevantes) e documentos (textos
longos, arquivos e páginas que o usuário mandou guardar), divididos em trechos e buscados por RAG.

Busca vetorial: os trechos vão para o motor configurado em vectorstore.py (numpy local ou Pinecone).
As memórias curtas (no máx. 300 por usuário) ficam sempre numa matriz numpy local, recarregada só
quando a "versão" (token no cache) muda.
"""
import hashlib
import logging
import re
import threading
import time
import uuid
from datetime import timedelta

import numpy as np
from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from apps.conf import cfg
from apps.errors import UserFacingError

from .embeddings import get_embedder
from .models import Chunk, Document, Memory, Retrieval
from .vectorstore import get_store

log = logging.getLogger(__name__)
_lock = threading.Lock()
_MEMS: dict[int, tuple] = {}
_QVEC: dict[tuple, object] = {}


# ----------------------------- versão (invalidação de cache) -----------------------------
# Token aleatório (não um contador): se o cache for limpo (ex.: Redis reiniciado), a versão nova nunca
# coincide com a de uma matriz antiga que ficou na memória do processo.
def version(user) -> str:
    return cache.get_or_set(f"kbv:{user.pk}", lambda: uuid.uuid4().hex[:12], None)


def bump_version(user):
    cache.set(f"kbv:{user.pk}", uuid.uuid4().hex[:12], None)


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
def _query_vec(query: str):
    """Vetor da pergunta, com cache curto: a cada pedido o runner busca memórias E trechos com o mesmo texto
    (com embeddings via API, isso economiza uma chamada de rede)."""
    emb = get_embedder()
    key = (emb.name, emb.dim, query)
    with _lock:
        hit = _QVEC.get(key)
    if hit is not None:
        return hit
    vec = emb.embed([query], kind="query")[0]
    with _lock:
        if len(_QVEC) > 256:
            _QVEC.clear()
        _QVEC[key] = vec
    return vec


def ingest(user, source_type: str, source: str, title: str, text: str, min_chars: int = 40) -> dict:
    text = (text or "").strip()[: cfg("MAX_INGEST_CHARS")]
    if len(text) < min_chars:
        return {"status": "skipped", "chunks": 0}
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    emb, vs = get_embedder(), get_store()
    try:
        with transaction.atomic():
            doc = Document.objects.filter(user=user, source_type=source_type, source=source).first()
            if doc and doc.content_hash == content_hash:
                return {"status": "unchanged", "chunks": doc.chunk_count, "document_id": doc.pk}
            pieces = chunk_text(text)
            existing = Chunk.objects.filter(user=user).count() - (doc.chunk_count if doc else 0)
            if existing + len(pieces) > cfg("MAX_CHUNKS"):
                raise UserFacingError("Base de conhecimento cheia. Remova fontes antigas (rag_forget).")
            vectors = emb.embed(pieces, kind="passage")
            old_count = doc.chunk_count if doc else 0
            if doc:
                doc.chunks.all().delete()
            else:
                doc = Document(user=user, source_type=source_type, source=source)
            doc.title, doc.content_hash, doc.chunk_count = (title or source)[:200], content_hash, len(pieces)
            doc.save()
            chunks = Chunk.objects.bulk_create([
                Chunk(document=doc, user=user, idx=i, text=p, embedding=vectors[i].tobytes())
                for i, p in enumerate(pieces)
            ])
            # ids estáveis (doc:idx): o upsert sobrescreve os vetores antigos; se falhar, o banco desfaz tudo
            vs.upsert(user, doc, chunks, vectors)
            if old_count > len(pieces):  # sobraram trechos da versão anterior (mais longa)
                vs.delete(user, [(doc.pk, len(pieces), old_count)])
    except UserFacingError:
        raise
    except Exception as exc:  # noqa: BLE001  (rede do Pinecone, cota da API de embeddings...)
        log.exception("Falha ao indexar %s", source)
        raise UserFacingError(f"Falha ao indexar no armazenamento vetorial ({type(exc).__name__}). Tente de novo.")
    bump_version(user)
    return {"status": "indexed", "chunks": len(pieces), "document_id": doc.pk}


def add_text(user, title: str, text: str) -> dict:
    """Guarda um texto livre (anotação, procedimento, trecho colado) como memória de conhecimento.
    O título identifica o texto: salvar de novo com o mesmo título atualiza o conteúdo."""
    text = (text or "").strip()
    if len(text) < 3:
        raise UserFacingError("Texto vazio.")
    title = " ".join((title or "").split())[:200] or " ".join(text.split())[:60]
    return ingest(user, "text", title, title, text, min_chars=1)


def maybe_learn(user, source_type, source, title, text):
    """Ingere conteúdo usado pelo agente SOMENTE se o usuário consentiu. Nunca quebra a ferramenta."""
    try:
        from apps.accounts.models import get_profile
        if get_profile(user).allow_learning:
            ingest(user, source_type, source, title, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("maybe_learn falhou: %s", exc)


def _delete_documents(user, qs) -> int:
    qs = qs.filter(user=user)
    docs = [(pk, 0, count) for pk, count in qs.values_list("pk", "chunk_count")]
    if not docs:
        return 0
    qs.delete()
    try:
        get_store().delete(user, docs)
    except Exception:  # noqa: BLE001  (vetores órfãos são ignorados na busca: o documento não existe mais)
        log.exception("Falha ao apagar vetores remotos")
    bump_version(user)
    return len(docs)


def forget_source(user, source: str) -> int:
    return _delete_documents(user, Document.objects.filter(source=source))


def forget_document(user, document_id: int) -> bool:
    return bool(_delete_documents(user, Document.objects.filter(pk=document_id)))


def list_documents(user, limit: int = 100) -> list:
    return [{"id": d.pk, "type": d.source_type, "source": d.source, "title": d.title, "chunks": d.chunk_count,
             "updated_at": d.updated_at.isoformat()}
            for d in Document.objects.filter(user=user)[:limit]]


def search(user, query: str, k: int = 4, min_score: float | None = None, origin: str = "", run=None):
    return search_traced(user, query, k, min_score, origin, run)["hits"]


def search_traced(user, query: str, k: int = 4, min_score: float | None = None, origin: str = "", run=None) -> dict:
    """Busca vetorial + rastreio: qual motor respondeu (pinecone/local), se houve fallback, tempo e notas.
    Com `origin` (auto | tool | test), a busca é registrada em Retrieval para auditoria no dashboard."""
    vs = get_store()
    info = {"hits": [], "store": vs.name, "fallback": False, "embedder": get_embedder().name, "ms": 0,
            "candidates": 0, "error": ""}
    if not query.strip() or not Chunk.objects.filter(user=user).exists():
        return info
    min_score = cfg("RAG_MIN_SCORE") if min_score is None else min_score
    start = time.perf_counter()
    try:
        qvec = _query_vec(query)
        try:
            hits = vs.search(user, qvec, k, version(user))
        except Exception as exc:  # noqa: BLE001
            if vs.name == "local":
                raise
            log.exception("Busca no %s falhou; usando a busca local", vs.name)
            info.update(fallback=True, store="local", error=f"{type(exc).__name__}: {str(exc)[:120]}")
            hits = get_store("local").search(user, qvec, k, version(user))
    except Exception as exc:  # noqa: BLE001  (API de embeddings fora do ar: o agente segue sem contexto)
        log.exception("Falha na busca vetorial")
        info["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        hits = []
    info["ms"] = int((time.perf_counter() - start) * 1000)
    info["candidates"] = len(hits)
    info["hits"] = [h for h in hits if h["score"] >= min_score]
    if origin:
        _log_retrieval(user, run, origin, query, info)
    return info


def _log_retrieval(user, run, origin, query, info):
    try:
        hits = info["hits"]
        Retrieval.objects.create(
            user=user, run=run, origin=origin, store=info["store"], fallback=info["fallback"],
            embedder=info["embedder"], query=query[:300], hits=len(hits), ms=info["ms"],
            top_score=max((h["score"] for h in hits), default=0),
            sources=[{"source": h["source"], "score": h["score"]} for h in hits[:8]])
    except Exception:  # noqa: BLE001  (auditoria nunca derruba a busca)
        log.exception("Falha ao registrar Retrieval")


def health(user) -> dict:
    """Confere se o armazenamento vetorial está acessível e em sincronia com o banco local (fonte da verdade)."""
    vs = get_store()
    out = {"store": vs.name, "embedder": get_embedder().name, "dimension": get_embedder().dim,
           "local_chunks": Chunk.objects.filter(user=user).count(), "ok": True}
    if vs.name != "pinecone":
        out.update(remote_vectors=out["local_chunks"], in_sync=True)
        return out
    start = time.perf_counter()
    try:
        out.update(vs.stats(user))
        out["in_sync"] = out["remote_vectors"] == out["local_chunks"]
    except Exception as exc:  # noqa: BLE001
        out.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")
    out["ms"] = int((time.perf_counter() - start) * 1000)
    return out


def rag_usage(user, days: int = 14) -> dict:
    """Métricas de uso do RAG para o dashboard: o agente está de fato recuperando da base vetorial?"""
    from apps.agent.models import Run
    since = timezone.now() - timedelta(days=days)
    rs = Retrieval.objects.filter(user=user, created_at__gte=since)
    agg = rs.aggregate(n=Count("id"), ms=Avg("ms"), top=Avg("top_score", filter=Q(hits__gt=0)))
    runs = Run.objects.filter(user=user, created_at__gte=since, cached=False).count()
    runs_ctx = rs.filter(origin="auto", hits__gt=0).values("run").distinct().count()
    by_store = {r["store"]: r["n"] for r in rs.values("store").annotate(n=Count("id"))}
    recent = [{"at": timezone.localtime(r.created_at).strftime("%d/%m %H:%M"), "origin": r.origin,
               "store": r.store, "fallback": r.fallback, "query": r.query[:80], "hits": r.hits,
               "top_score": round(r.top_score, 3), "ms": r.ms, "sources": r.sources[:3], "run_id": r.run_id}
              for r in rs[:10]]
    return {"searches": agg["n"] or 0, "with_hits": rs.filter(hits__gt=0).count(),
            "runs": runs, "runs_with_context": runs_ctx,
            "context_rate": round(100 * runs_ctx / runs, 1) if runs else 0,
            "avg_ms": int(agg["ms"] or 0), "avg_top_score": round(agg["top"] or 0, 3),
            "by_store": by_store, "fallbacks": rs.filter(fallback=True).count(),
            "tool_calls": rs.filter(origin="tool").count(), "recent": recent}


# ----------------------------- Memória -----------------------------
def _memory_matrix(user):
    ver = version(user)
    with _lock:
        cached = _MEMS.get(user.pk)
        if cached and cached[0] == ver and (cached[1] is None or cached[1].shape[1] == get_embedder().dim):
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
    vec = get_embedder().embed([content], kind="passage")[0]
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
    try:
        scores = mat @ _query_vec(query)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao vetorizar a busca de memórias")
        return []
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
        "vector_store": get_store().name,
        "index": cfg("PINECONE_INDEX") if get_store().name == "pinecone" else "",
        "vector_bytes": (chunks + memories) * emb.dim * 4,
        "by_type": [{"type": r["source_type"], "documents": r["n"], "chunks": r["chunks"] or 0} for r in by_type],
        "recent_sources": [{"id": d.pk, "type": d.source_type, "source": d.source, "chunks": d.chunk_count}
                           for d in docs[:5]],
    }
