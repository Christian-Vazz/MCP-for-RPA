from django.utils import timezone

from apps.agent.models import Run


def today_start():
    return timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)


def runs_today(user) -> int:
    """Conta solicitações do dia (inclusive as respondidas por cache, como mostra o dashboard)."""
    return Run.objects.filter(user=user, created_at__gte=today_start()).count()
