"""Ferramentas de RAG e de memória do agente."""
from apps.errors import UserFacingError as ToolError
from apps.knowledge import store
from apps.workspace import sandbox

from .registry import tool


@tool(category="knowledge", read_only=True, untrusted=True, required=["query"], params={
    "query": ("string", "Pergunta ou termos de busca"), "k": ("integer", "Quantidade de trechos (1-8)")})
def rag_search(ctx, query: str, k: int = 4):
    """Busca trechos relevantes nos arquivos e sites que o usuário já usou (base de conhecimento privada)."""
    results = store.search(ctx.user, query, k=max(1, min(int(k), 8)))
    return {"query": query, "results": results} if results else {"query": query, "results": [],
                                                                  "note": "Nada relevante na base."}


@tool(category="knowledge", required=["path"], params={"path": ("string", "Arquivo do workspace a indexar")})
def rag_ingest_file(ctx, path: str):
    """Indexa um arquivo do workspace na base de conhecimento (pedido explícito do usuário)."""
    text = sandbox.read_any(ctx.user, path)
    return store.ingest(ctx.user, "file", sandbox.clean_rel(path), path, text)


@tool(category="knowledge", read_only=True, params={})
def rag_stats(ctx):
    """Mostra quantos documentos, trechos e memórias existem na base do usuário."""
    return store.stats(ctx.user)


@tool(category="knowledge", required=["source"], params={"source": ("string", "Caminho do arquivo ou URL da fonte")})
def rag_forget(ctx, source: str):
    """Remove uma fonte (arquivo ou URL) da base de conhecimento."""
    n = store.forget_source(ctx.user, source)
    if not n:
        raise ToolError("Fonte não encontrada na base.")
    return {"removed": source}


@tool(category="memory", required=["content"], params={
    "content": ("string", "Fato curto e útil a lembrar (máx. 500 caracteres)"),
    "kind": ("string", "fact | preference | task | note"),
    "importance": ("integer", "1 (baixa) a 5 (alta)")})
def memory_save(ctx, content: str, kind: str = "fact", importance: int = 3):
    """Salva uma memória de longo prazo sobre o usuário (preferências, fatos estáveis, tarefas em andamento).
    Não salve senhas, documentos pessoais ou dados sensíveis."""
    return store.save_memory(ctx.user, content, kind, importance)


@tool(category="memory", read_only=True, required=["query"], params={"query": ("string", "Assunto a lembrar")})
def memory_search(ctx, query: str):
    """Procura nas memórias de longo prazo do usuário."""
    return {"memories": store.recall(ctx.user, query, k=6)}


@tool(category="memory", required=["memory_id"], params={"memory_id": ("integer", "ID da memória")})
def memory_forget(ctx, memory_id: int):
    """Apaga uma memória pelo ID."""
    if not store.forget_memory(ctx.user, memory_id):
        raise ToolError("Memória não encontrada.")
    return {"forgotten": memory_id}
