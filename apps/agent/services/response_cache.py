"""Cache de respostas: perguntas idênticas (1ª mensagem da conversa, sem uso de ferramentas)
não gastam tokens. A chave inclui as versões do workspace e da base de conhecimento, então o
cache se invalida sozinho quando arquivos/memórias mudam."""
import hashlib
import re

from django.core.cache import cache

from apps.conf import cfg
from apps.knowledge import store
from apps.workspace import sandbox

from .llm import provider_name

_TIME_SENSITIVE = re.compile(
    r"\b(hoje|agora|atual|atualmente|hora|horas|data|amanh[ãa]|ontem|semana|today|now|current|latest|[úu]ltim[oa]s?)\b", re.I)


def make_key(user, prompt: str) -> str:
    if _TIME_SENSITIVE.search(prompt) or len(prompt) > 600:
        return ""
    norm = " ".join(prompt.lower().split())
    raw = f"{user.pk}|{norm}|{provider_name()}|{cfg('MODEL')}|kb{store.version(user)}|ws{sandbox.version(user)}"
    return "resp:" + hashlib.sha256(raw.encode()).hexdigest()


def get(key: str):
    return cache.get(key) if key else None


def put(key: str, answer: str, tokens: int):
    if key and answer.strip():
        cache.set(key, {"answer": answer, "tokens": tokens}, cfg("RESPONSE_CACHE_TTL"))
