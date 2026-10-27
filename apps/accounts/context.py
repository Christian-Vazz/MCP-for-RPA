from .models import get_profile


def theme(request):
    """Tema salvo no perfil (usado só para o primeiro paint; o resto vem do localStorage)."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return {"user_theme": get_profile(user).theme}
    return {"user_theme": "dark"}
