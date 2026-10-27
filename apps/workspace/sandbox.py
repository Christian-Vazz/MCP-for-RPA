"""Sandbox de arquivos: TUDO que o agente (ou o usuário) faz com arquivos passa por aqui.

Garantias:
  * caminhos são normalizados e confinados à pasta do usuário (anti path-traversal/symlink);
  * nomes ocultos e pastas reservadas (.versions, .trash) são bloqueados;
  * só extensões da lista branca; edição só em texto, com limite de tamanho;
  * cota de disco, número máximo de arquivos e limite de edições por arquivo/dia;
  * toda sobrescrita gera uma versão (desfazer) e toda exclusão vai para a lixeira.
"""
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.conf import cfg
from apps.errors import UserFacingError as ToolError

from .models import FileEvent

RESERVED = {".versions", ".trash"}
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".py", ".html", ".css", ".js", ".xml", ".yaml", ".yml",
            ".log", ".ini", ".sql", ".toml", ".rst"}
READ_EXT = TEXT_EXT | {".pdf", ".docx", ".xlsx"}
UPLOAD_EXT = READ_EXT | {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_READ_CHARS = 300_000


# ----------------------------- caminhos -----------------------------
def user_root(user) -> Path:
    p = Path(settings.MEDIA_ROOT) / "workspaces" / str(user.pk)
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def clean_rel(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ToolError("Caminho inválido: '..' não é permitido.")
    if parts and parts[0] in RESERVED:
        raise ToolError("Pasta reservada do sistema.")
    if any(p.startswith(".") for p in parts):
        raise ToolError("Nomes ocultos (iniciados por '.') não são permitidos.")
    if len("/".join(parts)) > 250:
        raise ToolError("Caminho longo demais.")
    return "/".join(parts)


def resolve(user, rel: str, must_exist: bool = False) -> Path:
    root = user_root(user)
    target = (root / clean_rel(rel)).resolve()
    if target != root and root not in target.parents:
        raise ToolError("Caminho fora do workspace.")
    if must_exist and not target.exists():
        raise ToolError(f"Não encontrado: {rel}")
    return target


def _ext(rel: str) -> str:
    return Path(rel).suffix.lower()


# ----------------------------- versão/cache -----------------------------
def version(user) -> int:
    return cache.get_or_set(f"wsv:{user.pk}", 1, None)


def bump(user):
    key = f"wsv:{user.pk}"
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 2, None)


# ----------------------------- listagem e uso -----------------------------
def list_files(user, rel: str = "", limit: int = 300):
    ver = version(user)
    ck = f"wslist:{user.pk}:{ver}:{rel}:{limit}"
    hit = cache.get(ck)
    if hit is not None:
        return hit
    base = resolve(user, rel)
    root = user_root(user)
    files = []
    if base.is_dir():
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in RESERVED)
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append({
                    "path": p.relative_to(root).as_posix(),
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime, timezone.get_current_timezone()).isoformat(),
                    "ext": p.suffix.lower().lstrip("."),
                })
                if len(files) >= limit:
                    break
            if len(files) >= limit:
                break
    cache.set(ck, files, 120)
    return files


def usage(user) -> dict:
    ver = version(user)
    ck = f"wsusage:{user.pk}:{ver}"
    hit = cache.get(ck)
    if hit is not None:
        return hit
    total = count = 0
    for dirpath, dirnames, filenames in os.walk(user_root(user)):
        in_reserved = any(part in RESERVED for part in Path(dirpath).parts)
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
            if not in_reserved and not name.startswith("."):
                count += 1
    data = {"bytes": total, "files": count,
            "quota_bytes": cfg("WORKSPACE_QUOTA_MB") * 1024 * 1024, "max_files": cfg("MAX_FILES")}
    cache.set(ck, data, 120)
    return data


def _check_capacity(user, new_bytes: int, old_bytes: int, is_new: bool):
    u = usage(user)
    if is_new and u["files"] >= u["max_files"]:
        raise ToolError(f"Limite de {u['max_files']} arquivos atingido.")
    if u["bytes"] - old_bytes + new_bytes > u["quota_bytes"]:
        raise ToolError(f"Cota de {cfg('WORKSPACE_QUOTA_MB')} MB do workspace excedida.")


def _check_edit_rate(user, rel: str):
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    n = FileEvent.objects.filter(user=user, path=rel, created_at__gte=start,
                                 action__in=["write", "edit", "append"]).count()
    if n >= cfg("MAX_EDITS_PER_FILE_DAY"):
        raise ToolError(f"Limite de {cfg('MAX_EDITS_PER_FILE_DAY')} edições/dia para '{rel}' atingido.")


# ----------------------------- versões (desfazer) -----------------------------
def _versions_dir(user, rel: str) -> Path:
    return user_root(user) / ".versions" / rel


def _backup(user, path: Path, rel: str):
    vdir = _versions_dir(user, rel)
    vdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, vdir / datetime.now().strftime("%Y%m%d%H%M%S%f"))
    for old in sorted(vdir.iterdir())[: -cfg("VERSIONS_KEPT")]:
        old.unlink(missing_ok=True)


def can_undo(user, rel: str) -> bool:
    vdir = _versions_dir(user, clean_rel(rel))
    return vdir.is_dir() and any(vdir.iterdir())


def undo(user, rel: str) -> dict:
    rel = clean_rel(rel)
    vdir = _versions_dir(user, rel)
    versions = sorted(vdir.iterdir()) if vdir.is_dir() else []
    if not versions:
        raise ToolError("Não há versão anterior para restaurar.")
    path = resolve(user, rel)
    before = path.stat().st_size if path.exists() else 0
    latest = versions[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(latest, path)
    latest.unlink()
    FileEvent.objects.create(user=user, path=rel, action="undo", size_before=before, size_after=path.stat().st_size)
    bump(user)
    return {"path": rel, "restored_bytes": path.stat().st_size, "versions_left": len(versions) - 1}


# ----------------------------- leitura -----------------------------
def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        return path.read_bytes()[:MAX_READ_CHARS * 4].decode("utf-8", errors="replace")[:MAX_READ_CHARS]
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ToolError("Instale 'pypdf' para ler PDFs.")
        reader = PdfReader(str(path))
        out = []
        for i, page in enumerate(reader.pages[:60]):
            out.append(f"[página {i + 1}]\n{page.extract_text() or ''}")
        return "\n\n".join(out)[:MAX_READ_CHARS]
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise ToolError("Instale 'python-docx' para ler DOCX.")
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                parts.append("\t".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)[:MAX_READ_CHARS]
    if ext == ".xlsx":
        try:
            import openpyxl
        except ImportError:
            raise ToolError("Instale 'openpyxl' para ler XLSX.")
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets[:5]:
            out.append(f"[planilha {ws.title}]")
            for r, row in enumerate(ws.iter_rows(values_only=True)):
                if r >= 200:
                    out.append("…")
                    break
                out.append("\t".join("" if c is None else str(c) for c in row))
        return "\n".join(out)[:MAX_READ_CHARS]
    raise ToolError(f"Formato '{ext}' não pode ser lido como texto.")


def read_any(user, rel: str) -> str:
    path = resolve(user, rel, must_exist=True)
    if not path.is_file():
        raise ToolError(f"'{rel}' não é um arquivo.")
    if _ext(rel) not in READ_EXT:
        raise ToolError(f"Formato '{_ext(rel)}' não é legível como texto.")
    if path.stat().st_size > 25 * 1024 * 1024:
        raise ToolError("Arquivo grande demais para leitura.")
    return extract_text(path)


# ----------------------------- escrita -----------------------------
def write_text(user, rel: str, content: str, action: str = "write") -> dict:
    rel = clean_rel(rel)
    if not rel:
        raise ToolError("Informe o caminho do arquivo.")
    if _ext(rel) not in TEXT_EXT:
        raise ToolError(f"Edição permitida só para texto ({', '.join(sorted(TEXT_EXT))}).")
    data = content.encode("utf-8")
    if len(data) > cfg("MAX_EDITABLE_KB") * 1024:
        raise ToolError(f"Conteúdo maior que {cfg('MAX_EDITABLE_KB')} KB.")
    path = resolve(user, rel)
    if path.is_dir():
        raise ToolError("O caminho aponta para uma pasta.")
    exists = path.exists()
    old = path.stat().st_size if exists else 0
    _check_capacity(user, len(data), old, is_new=not exists)
    _check_edit_rate(user, rel)
    if exists:
        _backup(user, path, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    FileEvent.objects.create(user=user, path=rel, action=action, size_before=old, size_after=len(data))
    bump(user)
    return {"path": rel, "bytes": len(data), "created": not exists}


def edit_text(user, rel: str, old: str, new: str, replace_all: bool = False) -> dict:
    if not old:
        raise ToolError("'old_text' não pode ser vazio.")
    current = read_any(user, rel)
    if _ext(rel) not in TEXT_EXT:
        raise ToolError("Edição permitida só para arquivos de texto.")
    n = current.count(old)
    if n == 0:
        raise ToolError("Trecho não encontrado. Leia o arquivo de novo e copie o texto exato.")
    if n > 1 and not replace_all:
        raise ToolError(f"O trecho aparece {n} vezes. Inclua mais contexto ou use replace_all=true.")
    out = write_text(user, rel, current.replace(old, new) if replace_all else current.replace(old, new, 1), "edit")
    out["replacements"] = n if replace_all else 1
    return out


def save_bytes(user, rel: str, data: bytes, action: str = "upload") -> dict:
    rel = clean_rel(rel)
    if _ext(rel) not in UPLOAD_EXT:
        raise ToolError(f"Extensão '{_ext(rel) or '(nenhuma)'}' não permitida.")
    if len(data) > cfg("MAX_UPLOAD_MB") * 1024 * 1024:
        raise ToolError(f"Arquivo maior que {cfg('MAX_UPLOAD_MB')} MB.")
    path = resolve(user, rel)
    exists = path.exists()
    old = path.stat().st_size if exists else 0
    _check_capacity(user, len(data), old, is_new=not exists)
    if exists:
        _backup(user, path, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    FileEvent.objects.create(user=user, path=rel, action=action, size_before=old, size_after=len(data))
    bump(user)
    return {"path": rel, "bytes": len(data)}


def safe_name(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[^\w.\- ()]", "_", name).strip(" .")
    return name[:120] or "arquivo"


def save_upload(user, uploaded) -> dict:
    name = safe_name(uploaded.name)
    if _ext(name) not in UPLOAD_EXT:
        raise ToolError(f"Extensão '{_ext(name) or '(nenhuma)'}' não permitida.")
    if uploaded.size > cfg("MAX_UPLOAD_MB") * 1024 * 1024:
        raise ToolError(f"Arquivo maior que {cfg('MAX_UPLOAD_MB')} MB.")
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while resolve(user, candidate).exists():
        candidate = f"{stem} ({n}){ext}"
        n += 1
    return save_bytes(user, candidate, uploaded.read(), action="upload")


def delete(user, rel: str) -> dict:
    rel = clean_rel(rel)
    path = resolve(user, rel, must_exist=True)
    if not path.is_file():
        raise ToolError("Só é possível excluir arquivos (não pastas).")
    trash = user_root(user) / ".trash"
    trash.mkdir(exist_ok=True)
    size = path.stat().st_size
    shutil.move(str(path), trash / f"{datetime.now():%Y%m%d%H%M%S}__{rel.replace('/', '__')}")
    FileEvent.objects.create(user=user, path=rel, action="delete", size_before=size)
    bump(user)
    return {"path": rel, "moved_to_trash": True}


def move(user, src: str, dst: str) -> dict:
    src, dst = clean_rel(src), clean_rel(dst)
    sp, dp = resolve(user, src, must_exist=True), resolve(user, dst)
    if not sp.is_file():
        raise ToolError("Só é possível mover arquivos.")
    if dp.exists():
        raise ToolError(f"Já existe '{dst}'.")
    if _ext(dst) not in UPLOAD_EXT:
        raise ToolError(f"Extensão '{_ext(dst)}' não permitida.")
    dp.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(sp), dp)
    FileEvent.objects.create(user=user, path=dst, action="move", size_after=dp.stat().st_size)
    bump(user)
    return {"from": src, "to": dst}
