"""Configurações do Orbit Agent. Tudo que é limite/flag fica no dicionário ORBIT."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=None, cast=str):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    if cast is bool:
        return value.strip().lower() in ("1", "true", "yes", "on", "sim")
    return cast(value)


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG", True, bool)
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.agent",
    "apps.knowledge",
    "apps.workspace",
    "apps.dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context.theme",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 30},
    }
}

REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "orbit"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = BASE_DIR / "data"  # workspaces de usuários ficam em data/workspaces/<id>/

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "chat"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

ORBIT = {
    # ---- LLM ----
    "LLM_PROVIDER": env("LLM_PROVIDER", "auto"),  # auto | ollama | mock
    # Vazio = https://ollama.com se houver chave (Ollama Cloud), senão http://localhost:11434.
    # Não usamos OLLAMA_HOST porque o próprio daemon do Ollama lê essa variável com outro sentido (ex.: 0.0.0.0).
    "OLLAMA_BASE_URL": env("OLLAMA_BASE_URL", ""),
    "OLLAMA_API_KEY": env("OLLAMA_API_KEY", ""),
    "MODEL": env("OLLAMA_MODEL", "gpt-oss:120b"),
    "OLLAMA_THINK": env("OLLAMA_THINK", "medium"),     # raciocínio do gpt-oss: low | medium | high (vazio = padrão)
    "OLLAMA_NUM_CTX": env("OLLAMA_NUM_CTX", 32768, int),  # o padrão do Ollama local é curto demais p/ prompt + 30 ferramentas
    "LLM_TIMEOUT": env("LLM_TIMEOUT", 180, int),       # segundos sem receber nada do stream
    "MAX_OUTPUT_TOKENS": env("MAX_OUTPUT_TOKENS", 4096, int),  # inclui o raciocínio do gpt-oss
    # ---- Agente ----
    "MAX_STEPS": 12,            # voltas máximas do loop LLM -> ferramenta
    "MAX_RUN_TOKENS": 80_000,   # teto de tokens por execução
    "HISTORY_MESSAGES": 16,     # mensagens de histórico enviadas ao modelo
    "MAX_PROMPT_CHARS": 4000,
    "MAX_TOOL_OUTPUT": 6000,    # caracteres devolvidos ao modelo por ferramenta
    "APPROVAL_TIMEOUT": 120,    # segundos esperando o usuário aprovar
    "WORKERS": 4,
    "SYNC_RUNS": False,         # True nos testes: executa sem thread
    # ---- Cotas ----
    "DAILY_LIMIT": env("DAILY_LIMIT", 50, int),
    "DIRECT_CALLS_PER_MIN": 30,
    "LOGIN_MAX_ATTEMPTS": 5,
    # ---- Cache ----
    "RESPONSE_CACHE_TTL": 3600,
    "WEB_CACHE_TTL": 3600,
    # ---- Workspace / edição de arquivos ----
    "WORKSPACE_QUOTA_MB": 100,
    "MAX_FILES": 300,
    "MAX_UPLOAD_MB": 10,
    "MAX_EDITABLE_KB": 1024,
    "MAX_EDITS_PER_FILE_DAY": 25,
    "MAX_WRITES_PER_RUN": 15,
    "VERSIONS_KEPT": 5,
    # ---- Rede ----
    "ALLOW_HTTP": False,
    "ALLOWED_DOMAINS": [d for d in env("ALLOWED_DOMAINS", "").split(",") if d],  # vazio = qualquer domínio público
    "BLOCKED_DOMAINS": [d for d in env("BLOCKED_DOMAINS", "").split(",") if d],
    "WEB_MAX_BYTES": 1_500_000,
    # ---- Navegador / desktop ----
    "ENABLE_BROWSER": env("ENABLE_BROWSER", True, bool),
    "BROWSER_HEADLESS": env("BROWSER_HEADLESS", True, bool),
    "BROWSER_NO_SANDBOX": env("BROWSER_NO_SANDBOX", False, bool),
    "BROWSER_IDLE_SECONDS": 600,
    "ENABLE_DESKTOP": env("ENABLE_DESKTOP", False, bool),
    # ---- Conhecimento (RAG + memória) ----
    "EMBEDDING_BACKEND": env("EMBEDDING_BACKEND", "hash"),  # hash | st
    "EMBEDDING_MODEL": env("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"),
    "EMBEDDING_DIM": 512,
    "RAG_MIN_SCORE": 0.22,
    "MAX_CHUNKS": 5000,
    "MAX_INGEST_CHARS": 200_000,
    "MAX_MEMORIES": 300,
}
