from django.conf import settings
from django.db import models


class Document(models.Model):
    """Fonte ingerida na base de conhecimento: arquivo do workspace, página web ou texto livre salvo como memória."""
    SOURCES = [("file", "Arquivo"), ("web", "Web"), ("text", "Texto")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="documents")
    source_type = models.CharField(max_length=10, choices=SOURCES)
    source = models.CharField(max_length=500)
    title = models.CharField(max_length=200, blank=True)
    content_hash = models.CharField(max_length=64)
    chunk_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("user", "source_type", "source")]
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.source_type}:{self.source}"


class Chunk(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, db_index=True)
    idx = models.PositiveIntegerField()
    text = models.TextField()
    embedding = models.BinaryField()  # float32 normalizado (np.float32.tobytes())


class Memory(models.Model):
    """Memória de longo prazo do agente, por usuário."""
    KINDS = [("fact", "Fato"), ("preference", "Preferência"), ("task", "Tarefa"), ("note", "Nota")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memories")
    kind = models.CharField(max_length=12, choices=KINDS, default="fact")
    content = models.TextField()
    importance = models.PositiveSmallIntegerField(default=3)
    embedding = models.BinaryField()
    uses = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-importance", "-created_at"]


class Retrieval(models.Model):
    """Registro de cada busca vetorial (RAG): permite VERIFICAR se o agente está usando a base,
    em qual motor (Pinecone ou local), com que notas e de quais fontes."""
    ORIGINS = [("auto", "Contexto automático"), ("tool", "rag_search"), ("test", "Teste no dashboard")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="retrievals")
    run = models.ForeignKey("agent.Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="retrievals")
    origin = models.CharField(max_length=8, choices=ORIGINS)
    store = models.CharField(max_length=12)          # motor que respondeu: pinecone | local
    fallback = models.BooleanField(default=False)    # o Pinecone falhou e a busca caiu para o local
    embedder = models.CharField(max_length=20)
    query = models.CharField(max_length=300)
    hits = models.PositiveSmallIntegerField(default=0)
    top_score = models.FloatField(default=0)
    ms = models.PositiveIntegerField(default=0)
    sources = models.JSONField(default=list)         # [{"source", "score"}]
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
