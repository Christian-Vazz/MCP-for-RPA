"""Ferramentas de RAG e de memória do agente."""
from apps.errors import UserFacingError as ToolError
from apps.knowledge import store
from apps.workspace import sandbox

from .netpolicy import check_url
from .registry import tool
from .web import _download


@tool(category="knowledge", read_only=True, untrusted=True, required=["query"], params={
    "query": ("string", "Pergunta ou termos de busca"), "k": ("integer", "Quantidade de trechos (1-8)")})
def rag_search(ctx, query: str, k: int = 4):
    """Busca trechos relevantes nos arquivos e sites que o usuário já usou (base de conhecimento privada)."""
    info = store.search_traced(ctx.user, query, k=max(1, min(int(k), 8)), origin="tool", run=ctx.run)
    out = {"query": query, "results": info["hits"], "store": info["store"], "ms": info["ms"]}
    if info["fallback"]:
        out["store"] = "local (Pinecone indisponível)"
    if not info["hits"]:
        out["note"] = "Nada relevante na base."
    return out


@tool(category="knowledge", required=["path"], params={"path": ("string", "Arquivo do workspace a indexar")})
def rag_ingest_file(ctx, path: str):
    """Salva um arquivo do workspace na memória de conhecimento (txt, md, csv, pdf, docx, xlsx...), para ser
    encontrado depois por rag_search. Use quando o usuário pedir para guardar/memorizar/aprender um arquivo."""
    text = sandbox.read_any(ctx.user, path)
    return store.ingest(ctx.user, "file", sandbox.clean_rel(path), path, text, min_chars=1)


@tool(category="memory", required=["content"], params={
    "title": ("string", "Título curto que identifica o texto (mesmo título = atualiza)"),
    "content": ("string", "Texto a guardar (anotações, procedimento, trecho de documento...)")})
def memory_add_text(ctx, content: str, title: str = ""):
    """Salva um texto longo na memória de conhecimento, dividido em trechos e buscável por rag_search.
    Para fatos curtos sobre o usuário use memory_save. Não salve senhas ou dados sensíveis."""
    return store.add_text(ctx.user, title, content)


@tool(category="memory", required=["url"], params={"url": ("string", "Página pública https:// a guardar")})
def memory_add_url(ctx, url: str):
    """Baixa uma página pública (HTTPS) e salva o texto dela na memória de conhecimento."""
    url = check_url(url)
    title, text = _download(url)
    return {"url": url, "title": title, **store.ingest(ctx.user, "web", url, title, text, min_chars=1)}


@tool(category="knowledge", read_only=True, params={})
def rag_stats(ctx):
    """Mostra quantos documentos, trechos e memórias existem na base do usuário e lista as fontes salvas."""
    return {**store.stats(ctx.user), "documents_list": store.list_documents(ctx.user, 30)}


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
