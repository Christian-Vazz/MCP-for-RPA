from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_pragmas(sender, connection, **kwargs):
    """WAL permite leituras (polling da UI) enquanto a thread do agente escreve."""
    if connection.vendor == "sqlite":
        cur = connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA busy_timeout=20000;")


class AgentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agent"

    def ready(self):
        connection_created.connect(_sqlite_pragmas)
        from . import tools  # noqa: F401  (registra as ferramentas)
