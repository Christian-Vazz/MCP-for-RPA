from django.core.management.base import BaseCommand

from apps.knowledge.embeddings import get_embedder
from apps.knowledge.models import Document, Memory
from apps.knowledge.store import bump_version
from apps.knowledge.vectorstore import get_store


class Command(BaseCommand):
    help = ("Recalcula todos os embeddings e repovoa o armazenamento vetorial "
            "(use após trocar EMBEDDING_BACKEND/DIM ou VECTOR_STORE).")

    def handle(self, *args, **opts):
        emb, vs = get_embedder(), get_store()
        users = set()
        for doc in Document.objects.select_related("user").iterator(chunk_size=50):
            chunks = list(doc.chunks.order_by("idx"))
            if not chunks:
                continue
            vectors = emb.embed([c.text for c in chunks], kind="passage")  # um lote por documento
            for c, v in zip(chunks, vectors):
                c.embedding = v.tobytes()
            type(chunks[0]).objects.bulk_update(chunks, ["embedding"])
            vs.upsert(doc.user, doc, chunks, vectors)
            users.add(doc.user)
        for mem in Memory.objects.select_related("user").iterator(chunk_size=200):
            mem.embedding = emb.embed([mem.content], kind="passage")[0].tobytes()
            mem.save(update_fields=["embedding"])
            users.add(mem.user)
        for user in users:
            bump_version(user)
        self.stdout.write(self.style.SUCCESS(
            f"Reindexado: embeddings '{emb.name}' (dim={emb.dim}), armazenamento '{vs.name}'."))
