"""Política de rede (anti-SSRF) usada pela consulta web e pelo navegador."""
import ipaddress
import socket
from urllib.parse import urlparse

from apps.conf import cfg
from apps.errors import UserFacingError as ToolError


def _domain_match(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def check_url(url: str) -> str:
    parsed = urlparse(url.strip())
    allowed_schemes = {"https", "http"} if cfg("ALLOW_HTTP") else {"https"}
    if parsed.scheme not in allowed_schemes:
        raise ToolError("Somente URLs HTTPS são permitidas.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ToolError("URL inválida.")
    if parsed.port not in (None, 80, 443):
        raise ToolError("Porta não permitida.")
    if cfg("BLOCKED_DOMAINS") and _domain_match(host, cfg("BLOCKED_DOMAINS")):
        raise ToolError("Domínio bloqueado.")
    if cfg("ALLOWED_DOMAINS") and not _domain_match(host, cfg("ALLOWED_DOMAINS")):
        raise ToolError("Domínio fora da lista permitida.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ToolError("Não foi possível resolver o domínio.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:  # bloqueia loopback, rede privada, link-local, metadata cloud etc.
            raise ToolError("Endereços internos/privados não são permitidos.")
    return url.strip()
