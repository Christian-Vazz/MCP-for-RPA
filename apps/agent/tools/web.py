"""Consulta web segura (HTTPS, anti-SSRF, limite de tamanho, cache)."""
import hashlib
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests
from django.core.cache import cache

from apps.conf import cfg
from apps.errors import UserFacingError as ToolError
from apps.knowledge import store

from .netpolicy import check_url
from .registry import tool

ALLOWED_TYPES = {"text/html", "text/plain", "application/json", "application/xhtml+xml", "text/markdown",
                 "application/xml", "text/xml"}
_BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}
_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "nav", "footer"}


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.parts, self._skip, self._in_title = "", [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data)


def html_to_text(html: str):
    p = _Extractor()
    p.feed(html)
    text = "".join(p.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return p.title.strip()[:200], "\n".join(line for line in lines if line)


def _download(url: str) -> tuple[str, str]:
    session = requests.Session()
    headers = {"User-Agent": "OrbitAgent/1.0 (projeto educacional)", "Accept": "text/html,text/plain,application/json;q=0.9"}
    hops = 0
    while True:
        check_url(url)  # revalida a cada redirecionamento
        resp = session.get(url, headers=headers, timeout=(5, 12), stream=True, allow_redirects=False)
        if resp.is_redirect and hops < 3:
            url = urljoin(url, resp.headers.get("Location", ""))
            hops += 1
            resp.close()
            continue
        break
    if resp.status_code >= 400:
        raise ToolError(f"O site respondeu HTTP {resp.status_code}.")
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_TYPES:
        raise ToolError(f"Tipo de conteúdo não suportado: {ctype or 'desconhecido'}.")
    raw, limit = b"", cfg("WEB_MAX_BYTES")
    for part in resp.iter_content(65536):
        raw += part
        if len(raw) > limit:
            break
    body = raw[:limit].decode(resp.encoding or "utf-8", errors="replace")
    if ctype in ("text/html", "application/xhtml+xml"):
        title, text = html_to_text(body)
    else:
        title, text = "", body
    return title or url, text


@tool(category="web", read_only=True, untrusted=True, required=["url"], params={
    "url": ("string", "URL https:// pública"),
    "max_chars": ("integer", "Máximo de caracteres retornados (padrão 5000)")})
def web_fetch(ctx, url: str, max_chars: int = 5000):
    """Baixa uma página pública (HTTPS) e devolve o texto limpo. Resultados ficam em cache por 1 hora."""
    url = check_url(url)
    key = "web:" + hashlib.sha256(url.encode()).hexdigest()
    cached = cache.get(key)
    hit = cached is not None
    title, text = cached if hit else _download(url)
    if not hit:
        cache.set(key, (title, text), cfg("WEB_CACHE_TTL"))
    store.maybe_learn(ctx.user, "web", url, title, text)
    max_chars = max(500, min(int(max_chars), 20000))
    return {"url": url, "title": title, "cached": hit, "total_chars": len(text),
            "text": text[:max_chars], "truncated": len(text) > max_chars}
