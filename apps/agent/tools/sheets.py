"""Ferramentas de planilha (CSV/TSV/XLSX) com pandas: inspecionar, consultar, agregar, criar e modificar.

Segurança: filtros e fórmulas usam a mini-linguagem do pandas (df.query/df.eval), que aceita mais do que
o necessário (ex.: `@variavel`, atributos arbitrários). Por isso toda expressão passa por `_check_expr`,
que só permite comparações, aritmética, nomes de coluna, literais e alguns métodos de texto/data.
Toda escrita passa pelo sandbox (versão para desfazer, cota, limite de edições por arquivo/dia).
"""
import ast
import csv
import io
import json
import re
from pathlib import Path

import pandas as pd

from apps.errors import UserFacingError as ToolError
from apps.workspace import sandbox

from .files import _count_write
from .registry import tool

SHEET_EXT = {".csv", ".tsv", ".xlsx"}
MAX_ROWS = 100_000
CSV_SHEET = "dados"  # nome lógico da única "planilha" de um CSV
AGG_FUNCS = {"sum", "mean", "min", "max", "count", "median", "nunique", "std"}

_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.Invert,
          ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr,
          ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
          ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Attribute, ast.Call, ast.keyword)
_ATTRS = {"str", "dt", "contains", "startswith", "endswith", "lower", "upper", "strip", "len", "isna", "notna",
          "isnull", "notnull", "isin", "between", "abs", "round", "year", "month", "day", "fillna"}


# ----------------------------------------------------------------- expressões
def _check_expr(expr: str) -> str:
    expr = (expr or "").strip()
    if len(expr) > 400:
        raise ToolError("Expressão longa demais (máx. 400 caracteres).")
    if "@" in expr or "__" in expr:
        raise ToolError("Expressão inválida: '@' e '__' não são permitidos.")
    # colunas com espaço usam crases (`Valor Total`): troca por um nome neutro só para validar a sintaxe
    probe = re.sub(r"`[^`]+`", "COLUNA", expr)
    try:
        tree = ast.parse(probe, mode="eval")
    except SyntaxError:
        raise ToolError(f"Expressão inválida: {expr}")
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            raise ToolError(f"Expressão não permitida ({type(node).__name__}). Use comparações, contas e colunas.")
        if isinstance(node, ast.Attribute) and node.attr not in _ATTRS:
            raise ToolError(f"Método/atributo não permitido: .{node.attr}")
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Attribute):
            raise ToolError("Só são permitidos métodos de coluna (ex.: nome.str.contains('x')).")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ToolError(f"Nome inválido: {node.id}")
    return expr


# sem variáveis externas: nomes só resolvem para colunas
_SAFE = {"engine": "python", "local_dict": {}, "global_dict": {}}


def _filter(df: pd.DataFrame, expr: str) -> pd.DataFrame:
    if not expr:
        return df
    try:
        return df.query(_check_expr(expr), **_SAFE)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001  (coluna inexistente, tipos incompatíveis...)
        raise ToolError(f"Filtro inválido ({type(exc).__name__}): {str(exc)[:200]}")


def _mask(df: pd.DataFrame, expr: str) -> pd.Series:
    return df.index.isin(_filter(df, expr).index)


def _compute(df: pd.DataFrame, expr: str):
    try:
        return df.eval(_check_expr(expr), **_SAFE)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Fórmula inválida ({type(exc).__name__}): {str(exc)[:200]}")


# ----------------------------------------------------------------- argumentos
def _json_arg(raw, name: str):
    """Aceita JSON ou o repr Python de dict/list (o registro converte objetos em str)."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            raise ToolError(f"'{name}' deve ser JSON válido.")


def _names(raw) -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [str(c).strip() for c in items if str(c).strip()]


def _records(df: pd.DataFrame, limit: int) -> list:
    return json.loads(df.head(limit).to_json(orient="records", date_format="iso", force_ascii=False))


def _require_cols(df, cols):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ToolError(f"Coluna(s) inexistente(s): {', '.join(missing)}. Existentes: {', '.join(map(str, df.columns))}")


# ----------------------------------------------------------------- leitura/escrita
class Book:
    """Planilha carregada: todas as abas (para regravar o .xlsx sem perder nenhuma) + formato do CSV."""

    def __init__(self, path, fmt, frames, sep=",", decimal="."):
        self.path, self.fmt, self.frames, self.sep, self.decimal = path, fmt, frames, sep, decimal

    def pick(self, sheet: str = "") -> str:
        if not sheet:
            return next(iter(self.frames))
        if sheet not in self.frames:
            raise ToolError(f"Aba '{sheet}' não existe. Abas: {', '.join(self.frames)}")
        return sheet


def _ext(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext not in SHEET_EXT:
        raise ToolError("Use uma planilha .csv, .tsv ou .xlsx.")
    return ext


def _load(user, path: str) -> Book:
    ext = _ext(path)
    raw = sandbox.read_bytes(user, path)
    try:
        if ext == ".xlsx":
            frames = pd.read_excel(io.BytesIO(raw), sheet_name=None, engine="openpyxl")
            book = Book(path, "xlsx", frames)
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            sep = "\t" if ext == ".tsv" else ","
            if ext == ".csv":
                try:
                    sep = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
                except csv.Error:
                    pass
            decimal = "," if sep == ";" else "."  # padrão brasileiro: "1.234,56" em CSV separado por ";"
            df = pd.read_csv(io.StringIO(text), sep=sep, decimal=decimal, nrows=MAX_ROWS + 1)
            book = Book(path, "csv", {CSV_SHEET: df}, sep, decimal)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Não consegui abrir a planilha ({type(exc).__name__}): {str(exc)[:200]}")
    if any(len(df) > MAX_ROWS for df in book.frames.values()):
        raise ToolError(f"Planilha grande demais (máx. {MAX_ROWS} linhas por aba).")
    return book


def _save(ctx, book: Book, action: str = "edit") -> dict:
    _count_write(ctx)
    if book.fmt == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            for name, df in book.frames.items():
                df.to_excel(writer, sheet_name=str(name)[:31], index=False)
        out = sandbox.write_sheet_bytes(ctx.user, book.path, buf.getvalue(), action)
    else:
        df = next(iter(book.frames.values()))
        out = sandbox.write_text(ctx.user, book.path, df.to_csv(index=False, sep=book.sep, decimal=book.decimal), action)
    ctx.changed = True
    return out


def _new_book(path: str, sheet: str, df: pd.DataFrame) -> Book:
    if _ext(path) == ".xlsx":
        return Book(path, "xlsx", {sheet or "Planilha1": df})
    return Book(path, "csv", {CSV_SHEET: df}, "\t" if path.lower().endswith(".tsv") else ",")


def _exists(user, path: str) -> bool:
    return sandbox.resolve(user, path).exists()


def _query(df, filter="", columns="", sort_by="", descending=False, group_by="", aggregate=""):
    df = _filter(df, filter)
    groups = _names(group_by)
    if groups:
        _require_cols(df, groups)
        pairs = []
        for item in _names(aggregate):
            col, _, func = item.partition(":")
            col, func = col.strip(), (func.strip() or "sum").lower()
            if func not in AGG_FUNCS:
                raise ToolError(f"Agregação '{func}' inválida. Use: {', '.join(sorted(AGG_FUNCS))}")
            _require_cols(df, [col])
            pairs.append((col, func))
        # uma agregação por coluna mantém o nome original ("total"); várias viram "total_sum", "total_mean"...
        per_col = {c: sum(1 for c2, _ in pairs if c2 == c) for c, _ in pairs}
        specs = {(col if per_col[col] == 1 else f"{col}_{func}"): (col, func) for col, func in pairs}
        if specs:
            df = df.groupby(groups, dropna=False).agg(**specs).reset_index()
        else:
            df = df.groupby(groups, dropna=False).size().reset_index(name="linhas")
    cols = _names(columns)
    if cols:
        _require_cols(df, cols)
        df = df[cols]
    sort = _names(sort_by)
    if sort:
        _require_cols(df, sort)
        df = df.sort_values(sort, ascending=not descending)
    return df


QUERY_PARAMS = {
    "sheet": ("string", "Aba da planilha .xlsx (vazio = primeira)"),
    "filter": ("string", "Filtro pandas, ex.: valor > 100 and cidade == 'SP' ou nome.str.contains('Ana')"),
    "columns": ("string", "Colunas a mostrar, separadas por vírgula (vazio = todas)"),
    "sort_by": ("string", "Colunas de ordenação, separadas por vírgula"),
    "descending": ("boolean", "Ordem decrescente"),
    "group_by": ("string", "Agrupar por estas colunas (vírgula)"),
    "aggregate": ("string", "Agregações com group_by, ex.: valor:sum,id:count. O resultado mantém o nome da coluna "
                            "(valor); se a mesma coluna tiver várias, vira valor_sum, valor_mean..."),
}


# ----------------------------------------------------------------- ferramentas de leitura
@tool(category="sheets", read_only=True, untrusted=True, required=["path"], params={
    "path": ("string", "Planilha do workspace (.csv, .tsv ou .xlsx)"),
    "sheet": ("string", "Aba (.xlsx); vazio = primeira")})
def sheet_info(ctx, path: str, sheet: str = ""):
    """Mostra a estrutura de uma planilha: abas, colunas, tipos, vazios, primeiras linhas e estatísticas numéricas.
    Use antes de consultar ou modificar uma planilha."""
    book = _load(ctx.user, path)
    name = book.pick(sheet)
    df = book.frames[name]
    numeric = df.select_dtypes("number")
    stats = {c: {"min": numeric[c].min(), "max": numeric[c].max(), "mean": round(float(numeric[c].mean()), 4),
                 "sum": numeric[c].sum()} for c in numeric.columns if numeric[c].notna().any()}
    return {"path": path, "format": book.fmt, "sheets": [{"name": n, "rows": len(f), "columns": len(f.columns)}
                                                          for n, f in book.frames.items()],
            "sheet": name, "rows": len(df),
            "columns": [{"name": str(c), "dtype": str(df[c].dtype), "empty": int(df[c].isna().sum())} for c in df.columns],
            "head": _records(df, 8), "numeric_stats": json.loads(json.dumps(stats, default=float))}


@tool(category="sheets", read_only=True, untrusted=True, required=["path"], params={
    "path": ("string", "Planilha do workspace"), **QUERY_PARAMS,
    "limit": ("integer", "Máximo de linhas retornadas (padrão 30, máx. 200)")})
def sheet_query(ctx, path: str, sheet: str = "", filter: str = "", columns: str = "", sort_by: str = "",
                descending: bool = False, group_by: str = "", aggregate: str = "", limit: int = 30):
    """Consulta uma planilha com pandas: filtra linhas, escolhe colunas, ordena e agrupa/agrega (soma, média...).
    Não altera o arquivo."""
    book = _load(ctx.user, path)
    df = _query(book.frames[book.pick(sheet)], filter, columns, sort_by, descending, group_by, aggregate)
    limit = max(1, min(int(limit), 200))
    return {"path": path, "rows_total": len(df), "rows_returned": min(limit, len(df)),
            "columns": [str(c) for c in df.columns], "rows": _records(df, limit)}


# ----------------------------------------------------------------- ferramentas de escrita
@tool(category="sheets", required=["path", "columns"], params={
    "path": ("string", "Nova planilha (.csv, .tsv ou .xlsx)"),
    "columns": ("string", "Nomes das colunas, separados por vírgula"),
    "rows": ("string", "Linhas iniciais em JSON: lista de objetos {coluna: valor} (opcional)"),
    "sheet": ("string", "Nome da aba (.xlsx)")})
def sheet_create(ctx, path: str, columns: str, rows: str = "", sheet: str = ""):
    """Cria uma planilha nova com as colunas (e linhas) informadas."""
    _ext(path)
    if _exists(ctx.user, path):
        raise ToolError(f"'{path}' já existe. Use as ferramentas de edição de planilha.")
    cols = _names(columns)
    if not cols:
        raise ToolError("Informe ao menos uma coluna.")
    data = _json_arg(rows, "rows") if rows else []
    if not isinstance(data, list):
        raise ToolError("'rows' deve ser uma lista de objetos.")
    df = pd.DataFrame(data, columns=cols)
    out = _save(ctx, _new_book(sandbox.clean_rel(path), sheet, df), "write")
    return {**out, "rows": len(df), "columns": cols}


@tool(category="sheets", required=["path", "rows"], params={
    "path": ("string", "Planilha do workspace"),
    "rows": ("string", "JSON: lista de objetos {coluna: valor} a acrescentar (máx. 500)"),
    "sheet": ("string", "Aba (.xlsx)")})
def sheet_add_rows(ctx, path: str, rows: str, sheet: str = ""):
    """Acrescenta linhas ao final de uma planilha (versão anterior fica disponível para desfazer)."""
    book = _load(ctx.user, path)
    name = book.pick(sheet)
    data = _json_arg(rows, "rows")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data or not all(isinstance(r, dict) for r in data):
        raise ToolError("'rows' deve ser uma lista de objetos {coluna: valor}.")
    if len(data) > 500:
        raise ToolError("Máximo de 500 linhas por chamada.")
    df = book.frames[name]
    _require_cols(df, sorted({k for r in data for k in r}))  # evita criar coluna nova por erro de digitação
    book.frames[name] = pd.concat([df, pd.DataFrame(data)], ignore_index=True)
    return {**_save(ctx, book), "added": len(data), "rows": len(book.frames[name])}


@tool(category="sheets", required=["path", "values"], params={
    "path": ("string", "Planilha do workspace"),
    "values": ("string", "JSON {coluna: valor}. Valor iniciado por '=' é fórmula, ex.: {\"total\": \"=preco * qtd\"}"),
    "filter": ("string", "Quais linhas alterar (filtro pandas). Obrigatório, a menos que all_rows=true"),
    "all_rows": ("boolean", "Alterar todas as linhas"),
    "sheet": ("string", "Aba (.xlsx)")})
def sheet_update(ctx, path: str, values: str, filter: str = "", all_rows: bool = False, sheet: str = ""):
    """Altera valores de células nas linhas que atendem ao filtro (ou em todas, com all_rows=true)."""
    if not filter and not all_rows:
        raise ToolError("Informe um filtro ou all_rows=true.")
    book = _load(ctx.user, path)
    name = book.pick(sheet)
    df = book.frames[name].copy()
    changes = _json_arg(values, "values")
    if not isinstance(changes, dict) or not changes:
        raise ToolError("'values' deve ser um objeto {coluna: valor}.")
    _require_cols(df, list(changes))
    mask = _mask(df, filter) if filter else df.index == df.index
    for col, val in changes.items():
        new = _compute(df, val[1:])[mask] if isinstance(val, str) and val.startswith("=") else val
        try:
            df.loc[mask, col] = new
        except (TypeError, ValueError):  # tipo incompatível (ex.: texto em coluna numérica): vira coluna genérica
            df[col] = df[col].astype(object)
            df.loc[mask, col] = new
    book.frames[name] = df
    return {**_save(ctx, book), "updated_rows": int(mask.sum()), "columns": list(changes)}


@tool(category="sheets", required=["path", "filter"], params={
    "path": ("string", "Planilha do workspace"),
    "filter": ("string", "Linhas a remover (filtro pandas), ex.: status == 'cancelado'"),
    "sheet": ("string", "Aba (.xlsx)")})
def sheet_delete_rows(ctx, path: str, filter: str, sheet: str = ""):
    """Remove as linhas que atendem ao filtro (a versão anterior fica disponível para desfazer)."""
    book = _load(ctx.user, path)
    name = book.pick(sheet)
    df = book.frames[name]
    mask = _mask(df, filter)
    if not mask.any():
        raise ToolError("Nenhuma linha atende ao filtro; nada foi alterado.")
    book.frames[name] = df[~mask].reset_index(drop=True)
    return {**_save(ctx, book), "deleted_rows": int(mask.sum()), "rows": len(book.frames[name])}


@tool(category="sheets", required=["path", "name"], params={
    "path": ("string", "Planilha do workspace"),
    "name": ("string", "Nome da nova coluna"),
    "expression": ("string", "Fórmula com outras colunas, ex.: preco * quantidade"),
    "value": ("string", "Valor fixo (se não houver fórmula)"),
    "sheet": ("string", "Aba (.xlsx)")})
def sheet_add_column(ctx, path: str, name: str, expression: str = "", value: str = "", sheet: str = ""):
    """Cria uma coluna calculada (fórmula sobre outras colunas) ou com um valor fixo."""
    book = _load(ctx.user, path)
    sname = book.pick(sheet)
    df = book.frames[sname].copy()
    if name in df.columns:
        raise ToolError(f"A coluna '{name}' já existe. Use sheet_update para alterá-la.")
    df[name] = _compute(df, expression) if expression else value
    book.frames[sname] = df
    return {**_save(ctx, book), "column": name, "preview": _records(df[[name]], 5)}


@tool(category="sheets", required=["path"], params={
    "path": ("string", "Planilha do workspace"),
    "rename": ("string", "JSON {nome_atual: nome_novo}"),
    "drop": ("string", "Colunas a remover, separadas por vírgula"),
    "sheet": ("string", "Aba (.xlsx)")})
def sheet_edit_columns(ctx, path: str, rename: str = "", drop: str = "", sheet: str = ""):
    """Renomeia e/ou remove colunas de uma planilha."""
    book = _load(ctx.user, path)
    name = book.pick(sheet)
    df = book.frames[name]
    mapping = _json_arg(rename, "rename") if rename else {}
    if not isinstance(mapping, dict):
        raise ToolError("'rename' deve ser um objeto {atual: novo}.")
    dropped = _names(drop)
    if not mapping and not dropped:
        raise ToolError("Informe 'rename' e/ou 'drop'.")
    _require_cols(df, list(mapping) + dropped)
    book.frames[name] = df.drop(columns=dropped).rename(columns=mapping)
    return {**_save(ctx, book), "columns": [str(c) for c in book.frames[name].columns]}


@tool(category="sheets", required=["path", "destination"], params={
    "path": ("string", "Planilha de origem"),
    "destination": ("string", "Arquivo de destino (.csv, .tsv ou .xlsx)"),
    **QUERY_PARAMS,
    "destination_sheet": ("string", "Aba de destino (.xlsx). Num .xlsx existente, cria/substitui só esta aba"),
    "overwrite": ("boolean", "Permite sobrescrever um destino existente")})
def sheet_save_as(ctx, path: str, destination: str, sheet: str = "", filter: str = "", columns: str = "",
                  sort_by: str = "", descending: bool = False, group_by: str = "", aggregate: str = "",
                  destination_sheet: str = "", overwrite: bool = False):
    """Salva o resultado de uma consulta (filtro, colunas, ordenação, agrupamento) em outro arquivo ou aba.
    Serve para relatórios/resumos e para converter CSV <-> XLSX."""
    book = _load(ctx.user, path)
    df = _query(book.frames[book.pick(sheet)], filter, columns, sort_by, descending, group_by, aggregate)
    dest = sandbox.clean_rel(destination)
    _ext(dest)
    if _exists(ctx.user, dest):
        if dest.lower().endswith(".xlsx") and destination_sheet:
            target = _load(ctx.user, dest)  # adiciona/substitui só a aba pedida, preservando as demais
            target.frames[destination_sheet] = df
        elif overwrite:
            target = _new_book(dest, destination_sheet, df)
        else:
            raise ToolError(f"'{dest}' já existe. Use overwrite=true ou outra aba (destination_sheet).")
    else:
        target = _new_book(dest, destination_sheet, df)
    return {**_save(ctx, target, "write"), "rows": len(df), "columns": [str(c) for c in df.columns]}
