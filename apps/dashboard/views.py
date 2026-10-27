from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.conf import cfg
from apps.http import api_login_required, error, json_body, rate_limited
from apps.knowledge import store

from . import stats


@login_required
@ensure_csrf_cookie
def dashboard(request):
    name = (request.user.email or request.user.username).split("@")[0]
    return render(request, "dashboard/dashboard.html", {"display_name": name})


@api_login_required
@require_GET
def dashboard_data(request):
    return JsonResponse(stats.build(request.user))


@api_login_required
@require_GET
def rag_health(request):
    """Pinecone acessível? Quantos vetores há no namespace do usuário x quantos trechos no banco local?"""
    return JsonResponse(store.health(request.user))


@api_login_required
@require_POST
def rag_test(request):
    """Busca de teste (sem o modelo): mostra exatamente o que o agente recuperaria para esta pergunta."""
    query = (json_body(request).get("query") or "").strip()[:300]
    if not query:
        return error("Digite uma pergunta para testar.")
    if rate_limited(f"ragtest:{request.user.pk}", cfg("DIRECT_CALLS_PER_MIN")):
        return error("Muitos testes seguidos. Aguarde um instante.", 429)
    info = store.search_traced(request.user, query, k=5, min_score=0.0, origin="test")
    return JsonResponse({**{k: v for k, v in info.items() if k != "hits"}, "min_score": cfg("RAG_MIN_SCORE"),
                         "results": [{"source": h["source"], "title": h.get("title", ""), "type": h.get("type", ""),
                                      "score": h["score"], "used": h["score"] >= cfg("RAG_MIN_SCORE"),
                                      "snippet": " ".join(h["text"].split())[:220]} for h in info["hits"]]})
