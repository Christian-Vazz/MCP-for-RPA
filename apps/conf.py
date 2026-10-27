from django.conf import settings


def cfg(key):
    """Lê uma configuração em tempo de chamada (facilita override nos testes)."""
    return settings.ORBIT[key]
