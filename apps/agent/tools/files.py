"""Ferramentas de arquivos (sempre dentro do workspace do usuário)."""
import csv
import io
import statistics

from apps.conf import cfg
from apps.errors import UserFacingError as ToolError
from apps.knowledge import store
from apps.workspace import sandbox

from .registry import tool


def _count_write(ctx):
    if ctx.writes >= cfg("MAX_WRITES_PER_RUN"):
        raise ToolError(f"Limite de {cfg('MAX_WRITES_PER_RUN')} escritas por execução atingido. "
                        "Peça ao usuário para continuar em uma nova solicitação.")
    ctx.writes += 1


@tool(category="files", read_only=True, params={"path": ("string", "Pasta relativa ao workspace; vazio = raiz")})
def list_files(ctx, path: str = ""):
    """Lista os arquivos do workspace do usuário (caminho, tamanho, data)."""
    files = sandbox.list_files(ctx.user, path)
    return {"count": len(files), "files": files[:100], "truncated": len(files) > 100}


@tool(category="files", read_only=True, untrusted=True, required=["path"], params={
    "path": ("string", "Caminho do arquivo (texto, csv, json, pdf, docx ou xlsx)"),
    "offset": ("integer", "Caractere inicial (para ler arquivos longos em partes)"),
    "max_chars": ("integer", "Máximo de caracteres a retornar (padrão 6000)")})
def read_file(ctx, path: str, offset: int = 0, max_chars: int = 6000):
    """Lê o conteúdo de um arquivo do workspace. Suporta txt/md/csv/json/código, PDF, DOCX e XLSX."""
    text = sandbox.read_any(ctx.user, path)
    store.maybe_learn(ctx.user, "file", sandbox.clean_rel(path), path, text)
    max_chars = max(200, min(int(max_chars), 20000))
    chunk = text[offset: offset + max_chars]
    return {"path": path, "total_chars": len(text), "offset": offset, "content": chunk,
            "has_more": offset + max_chars < len(text)}


@tool(category="files", required=["path", "content"], params={
    "path": ("string", "Caminho do arquivo de texto a criar/sobrescrever"),
    "content": ("string", "Conteúdo completo do arquivo")})
def write_file(ctx, path: str, content: str):
    """Cria um arquivo de texto novo ou sobrescreve um existente (a versão anterior fica disponível para desfazer)."""
    _count_write(ctx)
    out = sandbox.write_text(ctx.user, path, content, "write")
    ctx.changed = True
    return out


@tool(category="files", required=["path", "old_text", "new_text"], params={
    "path": ("string", "Arquivo de texto a editar"),
    "old_text": ("string", "Trecho EXATO a substituir (deve ser único no arquivo)"),
    "new_text": ("string", "Texto novo"),
    "replace_all": ("boolean", "Substituir todas as ocorrências")})
def edit_file(ctx, path: str, old_text: str, new_text: str, replace_all: bool = False):
    """Edição cirúrgica: troca um trecho exato por outro. Prefira esta ferramenta a reescrever o arquivo inteiro."""
    _count_write(ctx)
    out = sandbox.edit_text(ctx.user, path, old_text, new_text, replace_all)
    ctx.changed = True
    return out


@tool(category="files", required=["path", "content"], params={
    "path": ("string", "Arquivo de texto"), "content": ("string", "Texto a acrescentar no final")})
def append_file(ctx, path: str, content: str):
    """Acrescenta texto ao final de um arquivo (cria se não existir)."""
    _count_write(ctx)
    try:
        current = sandbox.read_any(ctx.user, path)
    except ToolError:
        current = ""
    sep = "" if (not current or current.endswith("\n")) else "\n"
    out = sandbox.write_text(ctx.user, path, current + sep + content, "append")
    ctx.changed = True
    return out


@tool(category="files", required=["source", "destination"], params={
    "source": ("string", "Caminho atual"), "destination": ("string", "Novo caminho")})
def move_file(ctx, source: str, destination: str):
    """Move ou renomeia um arquivo dentro do workspace."""
    _count_write(ctx)
    out = sandbox.move(ctx.user, source, destination)
    ctx.changed = True
    return out


@tool(category="files", risk="high", required=["path"], params={"path": ("string", "Arquivo a excluir")})
def delete_file(ctx, path: str):
    """Exclui um arquivo (vai para a lixeira do workspace). Exige aprovação do usuário."""
    out = sandbox.delete(ctx.user, path)
    ctx.changed = True
    return out


@tool(category="files", required=["path"], params={"path": ("string", "Arquivo cuja última edição será desfeita")})
def undo_edit(ctx, path: str):
    """Restaura a versão anterior de um arquivo editado."""
    out = sandbox.undo(ctx.user, path)
    ctx.changed = True
    return out


@tool(category="files", read_only=True, untrusted=True, required=["query"], params={
    "query": ("string", "Texto a procurar (sem diferenciar maiúsculas) em nomes e conteúdos")})
def search_files(ctx, query: str):
    """Procura um texto nos nomes e no conteúdo dos arquivos de texto do workspace."""
    q = query.lower().strip()
    if not q:
        raise ToolError("Informe o texto a procurar.")
    hits = []
    for f in sandbox.list_files(ctx.user)[:200]:
        if q in f["path"].lower():
            hits.append({"path": f["path"], "match": "nome"})
        if f["ext"] and f".{f['ext']}" in sandbox.TEXT_EXT and f["size"] < 300_000:
            try:
                text = sandbox.read_any(ctx.user, f["path"])
            except ToolError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if q in line.lower():
                    hits.append({"path": f["path"], "line": n, "snippet": line.strip()[:160]})
                    break
        if len(hits) >= 20:
            break
    return {"query": query, "hits": hits}


@tool(category="files", read_only=True, untrusted=True, required=["path"], params={
    "path": ("string", "Arquivo .csv/.tsv do workspace")})
def csv_summary(ctx, path: str):
    """Resume uma tabela CSV: colunas, nº de linhas, primeiras linhas e estatísticas das colunas numéricas."""
    text = sandbox.read_any(ctx.user, path)
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))[:20000]
    if not rows:
        raise ToolError("CSV vazio ou ilegível.")
    cols = list(rows[0].keys())
    stats = {}
    for c in cols:
        vals = []
        for r in rows:
            try:
                vals.append(float(str(r.get(c, "")).replace(",", ".")))
            except ValueError:
                pass
        if len(vals) >= max(3, len(rows) * 0.6):
            stats[c] = {"min": min(vals), "max": max(vals), "mean": round(statistics.fmean(vals), 4)}
    empties = {c: sum(1 for r in rows if not (r.get(c) or "").strip()) for c in cols}
    return {"path": path, "rows": len(rows), "columns": cols, "head": rows[:5],
            "numeric_stats": stats, "empty_cells": {k: v for k, v in empties.items() if v}}
