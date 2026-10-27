"""Métricas do dashboard. Resultado em cache curto (5 s) para suportar auto-refresh sem custo."""
from collections import Counter
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.accounts.models import get_profile
from apps.agent.models import ActionLog, Message, Run
from apps.agent.services.llm import provider_name
from apps.agent.services.usage import runs_today, today_start
from apps.conf import cfg
from apps.knowledge import store
from apps.workspace import sandbox

DAYS = 14


def build(user) -> dict:
    key = f"dash:{user.pk}"
    data = cache.get(key)
    if data is not None:
        return data
    profile = get_profile(user)
    start = today_start()
    runs = Run.objects.filter(user=user)
    today_runs = runs.filter(created_at__gte=start)
    agg_today = today_runs.aggregate(i=Sum("input_tokens"), o=Sum("output_tokens"))
    agg_all = runs.aggregate(n=Count("id"), i=Sum("input_tokens"), o=Sum("output_tokens"),
                             cr=Sum("cache_read_tokens"), saved=Sum("saved_tokens"))
    total, cached = agg_all["n"] or 0, runs.filter(cached=True).count()
    errors = runs.filter(status="error").count()
    lat = runs.filter(status="done", cached=False, started_at__isnull=False, finished_at__isnull=False).aggregate(
        avg=Avg(ExpressionWrapper(F("finished_at") - F("started_at"), output_field=DurationField())))["avg"]

    # série dos últimos 14 dias
    since = start - timedelta(days=DAYS - 1)
    by_day = {r["d"]: r for r in runs.filter(created_at__gte=since).annotate(d=TruncDate("created_at"))
              .values("d").annotate(n=Count("id"), i=Sum("input_tokens"), o=Sum("output_tokens"))}
    series = []
    for k in range(DAYS):
        day = (since + timedelta(days=k)).date()
        row = by_day.get(day)
        series.append({"date": day.isoformat(), "runs": row["n"] if row else 0,
                       "tokens": ((row["i"] or 0) + (row["o"] or 0)) if row else 0})

    actions = ActionLog.objects.filter(user=user)
    ranking = Counter()
    failures = Counter()
    durations = Counter()
    for tool, ok, ms in actions.values_list("tool", "ok", "duration_ms"):
        ranking[tool] += 1
        durations[tool] += ms
        if not ok:
            failures[tool] += 1
    tools = [{"tool": t, "count": n, "failures": failures[t], "avg_ms": int(durations[t] / n)}
             for t, n in ranking.most_common(8)]

    ws = sandbox.usage(user)
    by_ext = Counter(f["ext"] or "outros" for f in sandbox.list_files(user))
    fb = Message.objects.filter(conversation__user=user, role="assistant")
    recent = [{"id": r.pk, "prompt": r.prompt[:70], "status": r.status, "cached": r.cached,
               "tokens": r.input_tokens + r.output_tokens, "steps": r.steps,
               "seconds": round((r.finished_at - r.started_at).total_seconds(), 1)
               if r.finished_at and r.started_at and not r.cached else 0,
               "at": timezone.localtime(r.created_at).strftime("%d/%m %H:%M")}
              for r in runs[:8]]

    data = {
        "provider": provider_name(), "model": cfg("MODEL") if provider_name() == "ollama" else "demonstração",
        "today": {"requests": runs_today(user), "limit": profile.daily_limit,
                  "tokens": (agg_today["i"] or 0) + (agg_today["o"] or 0),
                  "actions": actions.filter(created_at__gte=start).count()},
        "totals": {"requests": total, "tokens": (agg_all["i"] or 0) + (agg_all["o"] or 0),
                   "actions": actions.count(), "cache_hit_rate": round(100 * cached / total, 1) if total else 0,
                   "tokens_saved": (agg_all["saved"] or 0) + (agg_all["cr"] or 0),
                   "avg_seconds": round(lat.total_seconds(), 1) if lat else 0,
                   "error_rate": round(100 * errors / total, 1) if total else 0},
        "series": series, "tools": tools,
        "workspace": {**ws, "by_ext": [{"ext": e, "count": c} for e, c in by_ext.most_common(6)]},
        "knowledge": store.stats(user),
        "feedback": {"up": fb.filter(feedback=1).count(), "down": fb.filter(feedback=-1).count()},
        "learning": profile.allow_learning,
        "recent": recent,
    }
    cache.set(key, data, 5)
    return data
