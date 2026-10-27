from django.conf import settings
from django.db import models


class FileEvent(models.Model):
    """Auditoria de alterações. Também alimenta o limite de edições por arquivo/dia."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="file_events")
    path = models.CharField(max_length=300)
    action = models.CharField(max_length=12)  # write|edit|append|upload|delete|move|undo
    size_before = models.BigIntegerField(default=0)
    size_after = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "path", "created_at"])]
