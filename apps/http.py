"""Helpers HTTP compartilhados pelas views de API."""
import json
from functools import wraps

from django.core.cache import cache
from django.http import JsonResponse


def api_login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Sessão expirada. Entre novamente."}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def json_body(request):
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def error(message, status=400, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def rate_limited(key, limit, window=60):
    """Contador simples em cache. Retorna True quando excedeu o limite."""
    cache.add(key, 0, window)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, window)
        count = 1
    return count > limit
