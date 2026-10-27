from django.core.management.base import BaseCommand

from apps.knowledge.embeddings import get_embedder
from apps.knowledge.models import Chunk, Memory
from apps.knowledge.store import bump_version


class Command(BaseCommand):
    help = "Recalcula todos os embeddings (use após trocar EMBEDDING_BACKEND/DIM)."

    def handle(self, *args, **opts):
        emb = get_embedder()
        users = set()
        for chunk in Chunk.objects.iterator(chunk_size=200):
            chunk.embedding = emb.embed([chunk.text])[0].tobytes()
            chunk.save(update_fields=["embedding"])
            users.add(chunk.user)
        for mem in Memory.objects.iterator(chunk_size=200):
            mem.embedding = emb.embed([mem.content])[0].tobytes()
            mem.save(update_fields=["embedding"])
            users.add(mem.user)
        for user in users:
            bump_version(user)
        self.stdout.write(self.style.SUCCESS(f"Reindexado com backend '{emb.name}' (dim={emb.dim})."))
