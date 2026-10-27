from django.conf import settings
from django.db import models


def default_limit():
    return settings.ORBIT["DAILY_LIMIT"]


class Profile(models.Model):
    THEMES = [("dark", "Escuro"), ("light", "Claro"), ("system", "Sistema")]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    daily_limit = models.PositiveIntegerField(default=default_limit)
    # Consentimento (LGPD): só com isto o sistema aprende com sites/arquivos e guarda amostras.
    allow_learning = models.BooleanField(default=False)
    # Aprova automaticamente ações de risco médio (cliques/digitação no navegador).
    auto_approve = models.BooleanField(default=False)
    theme = models.CharField(max_length=10, choices=THEMES, default="dark")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Perfil de {self.user}"


def get_profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile
