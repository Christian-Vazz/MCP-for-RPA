"""Endpoints JSON (AJAX) do agente."""
from datetime import timedelta

from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.models import get_profile
from apps.agent.models import Approval, Conversation, Message, Run, RunEvent, TrainingSample
from apps.agent.services import response_cache, runner
from apps.agent.services.usage import runs_today
from apps.agent.tools import REGISTRY, ToolContext, available_tools, execute
from apps.conf import cfg
from apps.errors import UserFacingError
from apps.http import api_login_required, error, json_body, rate_limited
from apps.knowledge import store
from apps.knowledge.models import Memory
from apps.workspace import sandbox

STALE_AFTER = timedelta(seconds=180)


def _reap_stale(qs):
    """Marca como erro execuções sem batimento (ex.: servidor reiniciado no meio)."""
    limit = timezone.now() - STALE_AFTER
    for run in qs.filter(status__in=Run.ACTIVE, heartbeat__lt=limit):
        Run.objects.filter(pk=run.pk).update(status="error", error="Execução interrompida.", finished_at=timezone.now())
        seq = (run.events.order_by("-seq").values_list("seq", flat=True).first() or 0) + 1
        RunEvent.objects.create(run=run, seq=seq, kind="done", payload={"status": "error", "usage": {}})


# ------------------------------------------------------------------ runs
@api_login_required
@require_POST
def run_create(request):
    body = json_body(request)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return error("Escreva uma mensagem.")
    if len(prompt) > cfg("MAX_PROMPT_CHARS"):
        return error(f"Mensagem acima de {cfg('MAX_PROMPT_CHARS')} caracteres.")
    user = request.user
    profile = get_profile(user)

    _reap_stale(Run.objects.filter(user=user))
    active = Run.objects.filter(user=user, status__in=Run.ACTIVE).first()
    if active:
        return error("Já existe uma solicitação em andamento. Aguarde ou cancele.", 409, run_id=active.pk)
    used = runs_today(user)
    if used >= profile.daily_limit:
        return error("Limite diário de solicitações atingido. Ele reinicia à meia-noite.", 429,
                     used=used, limit=profile.daily_limit)

    if body.get("conversation_id"):
        conv = Conversation.objects.filter(pk=body["conversation_id"], user=user).first()
        if not conv:
            return error("Conversa não encontrada.", 404)
    else:
        conv = Conversation.objects.create(user=user, title=prompt[:60])
    first_turn = not conv.messages.exists()
    umsg = Message.objects.create(conversation=conv, role="user", content=prompt)

    key = response_cache.make_key(user, prompt) if first_turn else ""
    hit = response_cache.get(key)
    now = timezone.now()
    if hit:
        run = Run.objects.create(user=user, conversation=conv, user_message=umsg, prompt=prompt, status="done",
                                 cached=True, answer=hit["answer"], saved_tokens=hit["tokens"],
                                 started_at=now, finished_at=now)
        amsg = Message.objects.create(conversation=conv, role="assistant", content=hit["answer"], run=run)
        RunEvent.objects.create(run=run, seq=1, kind="text", payload={"text": hit["answer"]})
        RunEvent.objects.create(run=run, seq=2, kind="done", payload={
            "status": "done", "message_id": amsg.pk,
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cached": True, "saved_tokens": hit["tokens"]}})
        conv.save(update_fields=["updated_at"])
        return JsonResponse({"run_id": run.pk, "conversation_id": conv.pk, "cached": True})

    run = Run.objects.create(user=user, conversation=conv, user_message=umsg, prompt=prompt, cache_key=key)
    runner.submit(run.pk)
    return JsonResponse({"run_id": run.pk, "conversation_id": conv.pk, "cached": False})


@api_login_required
@require_GET
def run_events(request, run_id):
    run = get_object_or_404(Run, pk=run_id, user=request.user)
    try:
        after = int(request.GET.get("after", 0))
    except ValueError:
        after = 0
    _reap_stale(Run.objects.filter(pk=run.pk))
    run.refresh_from_db()
    events = list(run.events.filter(seq__gt=after).values("seq", "kind", "payload")[:200])
    terminal = run.status not in Run.ACTIVE
    return JsonResponse({
        "events": events, "status": run.status,
        "partial": run.partial if not terminal else "",
        "done": terminal and not events,
    })


@api_login_required
@require_POST
def run_cancel(request, run_id):
    n = Run.objects.filter(pk=run_id, user=request.user, status__in=Run.ACTIVE).update(status="cancelled")
    return JsonResponse({"cancelled": bool(n)})


@api_login_required
@require_POST
def approval_decide(request, approval_id):
    appr = get_object_or_404(Approval, pk=approval_id, run__user=request.user)
    decision = json_body(request).get("decision")
    if decision not in ("allow", "deny"):
        return error("Decisão inválida.")
    if appr.decision:
        return error("Esta ação já foi decidida.", 409)
    appr.decision, appr.decided_at = decision, timezone.now()
    appr.save(update_fields=["decision", "decided_at"])
    return JsonResponse({"decision": decision})


# ------------------------------------------------------------------ conversas
@api_login_required
@require_http_methods(["GET"])
def conversations(request):
    items = Conversation.objects.filter(user=request.user)[:50]
    return JsonResponse({"conversations": [
        {"id": c.pk, "title": c.title, "updated_at": c.updated_at.isoformat()} for c in items]})


@api_login_required
@require_http_methods(["GET", "DELETE"])
def conversation_detail(request, pk):
    conv = get_object_or_404(Conversation, pk=pk, user=request.user)
    if request.method == "DELETE":
        conv.delete()
        return JsonResponse({"deleted": True})
    _reap_stale(conv.runs.all())
    active = conv.runs.filter(status__in=Run.ACTIVE).first()
    return JsonResponse({
        "id": conv.pk, "title": conv.title,
        "active_run": active.pk if active else None,
        "messages": [{"id": m.pk, "role": m.role, "content": m.content, "feedback": m.feedback,
                      "run_id": m.run_id, "cached": bool(m.run_id and m.run and m.run.cached)}
                     for m in conv.messages.select_related("run")],
    })


@api_login_required
@require_POST
def message_feedback(request, pk):
    msg = get_object_or_404(Message, pk=pk, conversation__user=request.user, role="assistant")
    value = json_body(request).get("value")
    if value not in (-1, 0, 1):
        return error("Valor inválido.")
    msg.feedback = value
    msg.save(update_fields=["feedback"])
    if msg.run_id:
        TrainingSample.objects.filter(run_id=msg.run_id).update(rating=value)
    return JsonResponse({"feedback": value})


# ------------------------------------------------------------------ ferramentas (uso direto)
@api_login_required
@require_GET
def tools_list(request):
    return JsonResponse({"tools": [
        {"name": t.name, "description": t.description, "category": t.category, "risk": t.risk,
         "read_only": t.read_only, "schema": t.schema()}
        for t in sorted(available_tools(), key=lambda t: (t.category, t.name))]})


@api_login_required
@require_POST
def tool_run(request):
    body = json_body(request)
    name = body.get("name")
    if name not in REGISTRY or not REGISTRY[name].is_enabled():
        return error("Ferramenta desconhecida ou desativada.", 404)
    if rate_limited(f"direct:{request.user.pk}", cfg("DIRECT_CALLS_PER_MIN")):
        return error("Muitas chamadas. Aguarde um instante.", 429)
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    res = execute(name, args, ToolContext(user=request.user, source="direct"))
    return JsonResponse({"ok": res.ok, "output": res.text(20000), "changed": res.changed, "ms": res.ms})


# ------------------------------------------------------------------ memórias
def _add_memory(user, body: dict) -> dict:
    """type: fact (frase curta) | text (texto longo) | url (página pública) | file (arquivo do workspace)."""
    kind = body.get("type") or "fact"
    if kind == "fact":
        return store.save_memory(user, body.get("content", ""), body.get("kind", "note"), body.get("importance", 3))
    if kind == "text":
        return store.add_text(user, body.get("title", ""), body.get("content", ""))
    if kind in ("url", "file"):  # mesmas regras do agente (anti-SSRF, sandbox): reaproveita as ferramentas
        name, args = ("memory_add_url", {"url": body.get("url", "")}) if kind == "url" \
            else ("rag_ingest_file", {"path": body.get("path", "")})
        res = execute(name, args, ToolContext(user=user, source="direct"))
        if not res.ok:
            raise UserFacingError(res.data.get("error", "Falha ao salvar."))
        return res.data
    raise UserFacingError("Tipo de memória inválido.")


def _knowledge_snapshot(user) -> dict:
    items = Memory.objects.filter(user=user)[:100]
    return {"memories": [{"id": m.pk, "kind": m.kind, "content": m.content, "importance": m.importance,
                          "uses": m.uses} for m in items],
            "documents": store.list_documents(user)}


@api_login_required
@require_http_methods(["GET", "POST"])
def memories(request):
    if request.method == "POST":
        try:
            out = _add_memory(request.user, json_body(request))
        except UserFacingError as exc:
            return error(str(exc))
        return JsonResponse(out)
    return JsonResponse(_knowledge_snapshot(request.user))


@api_login_required
@require_POST
def memories_upload(request):
    """Envia arquivos para a pasta memoria/ do workspace e os indexa na memória de conhecimento."""
    results, errors = [], []
    for f in request.FILES.getlist("files")[:10]:
        try:
            saved = sandbox.save_upload(request.user, f, folder="memoria")
            if sandbox.extension(saved["path"]) not in sandbox.READ_EXT:
                raise UserFacingError("Formato salvo no workspace, mas não pode ser lido como texto (imagem?).")
            text = sandbox.read_any(request.user, saved["path"])
            results.append({**saved, **store.ingest(request.user, "file", saved["path"], saved["path"], text,
                                                    min_chars=1)})
        except UserFacingError as exc:
            errors.append({"name": f.name, "error": str(exc)})
    return JsonResponse({"saved": results, "errors": errors, **_knowledge_snapshot(request.user)})


@api_login_required
@require_http_methods(["DELETE"])
def memory_detail(request, pk):
    return JsonResponse({"deleted": store.forget_memory(request.user, pk)})


@api_login_required
@require_http_methods(["DELETE"])
def document_detail(request, pk):
    return JsonResponse({"deleted": store.forget_document(request.user, pk)})
