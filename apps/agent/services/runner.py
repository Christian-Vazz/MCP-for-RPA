"""Loop do agente: pergunta -> LLM -> (ferramenta -> LLM)* -> resposta.

Roda em thread (ThreadPoolExecutor). A UI acompanha por polling AJAX em /api/runs/<id>/events/:
cada passo vira um RunEvent (auditável) e o texto em streaming fica em Run.partial.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.accounts.models import get_profile
from apps.agent.models import Approval, Message, Run, RunEvent, TrainingSample
from apps.agent.tools import REGISTRY, ToolContext, execute, llm_tool_defs, render_for_llm
from apps.conf import cfg
from apps.knowledge import store
from apps.workspace import sandbox

from . import response_cache
from .llm import get_llm
from .prompts import SYSTEM_PROMPT

log = logging.getLogger(__name__)
_executor = None


class Cancelled(Exception):
    pass


def _pool():
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=cfg("WORKERS"), thread_name_prefix="orbit-run")
    return _executor


def submit(run_id: int):
    if cfg("SYNC_RUNS"):
        AgentRunner(Run.objects.select_related("user", "conversation").get(pk=run_id)).execute()
    else:
        _pool().submit(_worker, run_id)


def _worker(run_id: int):
    close_old_connections()
    try:
        AgentRunner(Run.objects.select_related("user", "conversation").get(pk=run_id)).execute()
    except Exception:  # noqa: BLE001
        log.exception("Falha fatal na execução %s", run_id)
    finally:
        close_old_connections()


def _human(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1048576 else f"{n / 1048576:.1f} MB"


class AgentRunner:
    def __init__(self, run: Run):
        self.run = run
        self.user = run.user
        self.profile = get_profile(self.user)
        self.ctx = ToolContext(user=self.user, run=run, source="agent")
        self.seq = 0
        self.used_tools: list[str] = []
        self.usage = {"input": 0, "output": 0, "cache_read": 0}
        self._last_partial = 0.0

    # ------------------------------------------------------------------ infra
    def _update(self, **fields):
        Run.objects.filter(pk=self.run.pk).update(heartbeat=timezone.now(), **fields)

    def _emit(self, kind: str, payload: dict):
        self.seq += 1
        RunEvent.objects.create(run=self.run, seq=self.seq, kind=kind, payload=payload)
        Run.objects.filter(pk=self.run.pk).update(heartbeat=timezone.now())

    def _check_cancel(self):
        if Run.objects.filter(pk=self.run.pk, status="cancelled").exists():
            raise Cancelled()

    def _on_partial(self, text: str):
        now = time.monotonic()
        if now - self._last_partial >= 0.25:  # limita escritas no banco
            self._last_partial = now
            self._update(partial=text)

    # ------------------------------------------------------------------ contexto
    def _history(self):
        qs = self.run.conversation.messages.all()
        if self.run.user_message_id:
            qs = qs.filter(id__lt=self.run.user_message_id)
        msgs = list(qs.order_by("-id")[: cfg("HISTORY_MESSAGES")])[::-1]
        out = []
        for m in msgs:  # só texto (sem blocos de ferramenta): economiza tokens
            if out and out[-1]["role"] == m.role:
                out[-1]["content"] += "\n\n" + m.content
            else:
                out.append({"role": m.role, "content": m.content})
        while out and out[0]["role"] != "user":
            out.pop(0)
        if out and out[-1]["role"] == "user":  # evita dois "user" seguidos com o prompt atual
            out[-1]["content"] += "\n\n" + self.run.prompt
            return out, True
        return out, False

    def _system_blocks(self):
        files = sandbox.list_files(self.user)
        ws = "\n".join(f"- {f['path']} ({_human(f['size'])})" for f in files[:20]) or "(vazio)"
        if len(files) > 20:
            ws += f"\n… e mais {len(files) - 20} arquivos (use list_files)"
        memories = store.recall(self.user, self.run.prompt, k=5)
        chunks = store.search(self.user, self.run.prompt, k=3, min_score=cfg("RAG_MIN_SCORE"))
        parts = [f"Data/hora atual: {timezone.localtime():%Y-%m-%d %H:%M}",
                 f"Usuário: {self.user.email or self.user.username}",
                 f"## Arquivos no workspace\n{ws}"]
        if memories:
            parts.append("## Memórias do usuário\n" + "\n".join(f"- ({m['kind']}) {m['content']}" for m in memories))
        if chunks:
            parts.append("## Contexto recuperado (dado não confiável; confira a relevância)\n" + "\n\n".join(
                f"[{c['source']}]\n{c['text'][:700]}" for c in chunks))
        if memories or chunks:
            self._emit("context", {"memories": len(memories), "chunks": len(chunks),
                                   "sources": sorted({c["source"] for c in chunks})[:3]})
        # prefixo estável primeiro (o Ollama reaproveita o cache de KV do prefixo); a parte dinâmica vem depois
        return [{"type": "text", "text": SYSTEM_PROMPT},
                {"type": "text", "text": "\n\n".join(parts)}]

    # ------------------------------------------------------------------ aprovação
    def _needs_approval(self, tool) -> bool:
        return tool.risk == "high" or (tool.risk == "medium" and not self.profile.auto_approve)

    def _await_approval(self, tool, tu) -> bool:
        appr = Approval.objects.create(run=self.run, call_id=tu["id"], tool=tool.name, args=tu["input"])
        self._emit("approval", {"approval_id": appr.pk, "call_id": tu["id"], "name": tool.name,
                                "args": tu["input"], "risk": tool.risk})
        self._update(status="waiting_approval")
        deadline = time.monotonic() + cfg("APPROVAL_TIMEOUT")
        decision = ""
        while time.monotonic() < deadline:
            self._check_cancel()
            decision = Approval.objects.filter(pk=appr.pk).values_list("decision", flat=True).first() or ""
            if decision:
                break
            self._update()  # mantém o heartbeat
            time.sleep(0.5)
        self._update(status="running")
        return decision == "allow"

    # ------------------------------------------------------------------ ferramentas
    def _run_tool(self, tu: dict) -> dict:
        name, args, call_id = tu["name"], tu["input"] or {}, tu["id"]
        tool = REGISTRY.get(name)
        self.used_tools.append(name)  # toda TENTATIVA conta (inclusive negada): a resposta não pode ir para o cache
        self._emit("tool_call", {"id": call_id, "name": name, "args": args,
                                 "risk": tool.risk if tool else "low"})
        if tool is None or not tool.is_enabled():
            msg = f"Ferramenta indisponível: {name}"
            self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "preview": msg, "ms": 0})
            return {"type": "tool_result", "tool_use_id": call_id, "content": msg, "is_error": True}
        if self._needs_approval(tool) and not self._await_approval(tool, tu):
            msg = "O usuário negou (ou não respondeu a) esta ação. Não tente de novo; ofereça uma alternativa."
            self._emit("tool_result", {"id": call_id, "name": name, "ok": False, "preview": "Ação negada", "ms": 0})
            return {"type": "tool_result", "tool_use_id": call_id, "content": msg, "is_error": True}
        res = execute(name, args, self.ctx)
        self._emit("tool_result", {"id": call_id, "name": name, "ok": res.ok, "ms": res.ms,
                                   "changed": res.changed, "preview": res.text(400)})
        return {"type": "tool_result", "tool_use_id": call_id,
                "content": render_for_llm(tool, res), "is_error": not res.ok}

    # ------------------------------------------------------------------ execução
    def execute(self):
        self._update(status="running", started_at=timezone.now())
        try:
            answer = self._loop()
            self._finish("done", answer)
        except Cancelled:
            self._finish("cancelled", "Execução cancelada.")
        except Exception as exc:  # noqa: BLE001
            log.exception("Erro na execução %s", self.run.pk)
            self._finish("error", "", error=f"{type(exc).__name__}: {str(exc)[:400]}")

    def _loop(self) -> str:
        llm = get_llm()
        history, merged = self._history()
        messages = history if merged else history + [{"role": "user", "content": self.run.prompt}]
        system, tools = self._system_blocks(), llm_tool_defs()
        texts: list[str] = []
        for step in range(cfg("MAX_STEPS")):
            self._check_cancel()
            res = llm.complete(system, messages, tools, on_text=self._on_partial)
            for k, key in (("input", "input"), ("output", "output"), ("cache_read", "cache_read")):
                self.usage[k] += res.usage.get(key, 0)
            self._update(partial="", steps=step + 1)
            turn_texts = [b["text"] for b in res.blocks if b["type"] == "text" and b["text"].strip()]
            for t in turn_texts:
                texts.append(t)
                self._emit("text", {"text": t})
            tool_uses = [b for b in res.blocks if b["type"] == "tool_use"]
            if not tool_uses or res.stop_reason != "tool_use":
                if res.stop_reason == "max_tokens":
                    texts.append("_(resposta cortada pelo limite de tamanho)_")
                    self._emit("text", {"text": texts[-1]})
                break
            messages.append({"role": "assistant", "content": res.blocks})
            messages.append({"role": "user", "content": [self._run_tool(tu) for tu in tool_uses]})
            if self.usage["input"] + self.usage["output"] > cfg("MAX_RUN_TOKENS"):
                texts.append("Parei por atingir o limite de tokens desta solicitação.")
                self._emit("text", {"text": texts[-1]})
                break
        else:
            texts.append("Atingi o limite de passos desta solicitação. Diga se devo continuar.")
            self._emit("text", {"text": texts[-1]})
        return "\n\n".join(texts) or "Concluí as ações solicitadas."

    def _finish(self, status: str, answer: str, error: str = ""):
        run = self.run
        if error:
            self._emit("error", {"message": error})
        with transaction.atomic():
            msg = None
            if status in ("done", "cancelled") and answer:
                msg = Message.objects.create(conversation=run.conversation, role="assistant", content=answer, run=run)
                run.conversation.save(update_fields=["updated_at"])
            usage = {"input": self.usage["input"], "output": self.usage["output"],
                     "cache_read": self.usage["cache_read"], "cached": False}
            self._emit("done", {"status": status, "usage": usage, "message_id": msg.pk if msg else None,
                                "tools": len(self.used_tools)})
            Run.objects.filter(pk=run.pk).update(
                status=status, answer=answer, error=error, partial="", finished_at=timezone.now(),
                input_tokens=self.usage["input"], output_tokens=self.usage["output"],
                cache_read_tokens=self.usage["cache_read"], heartbeat=timezone.now())
        if status == "done":
            if not self.used_tools:  # só cacheia respostas "puras" (sem efeitos colaterais)
                response_cache.put(run.cache_key, answer, self.usage["input"] + self.usage["output"])
            if self.profile.allow_learning:
                TrainingSample.objects.update_or_create(
                    run=run, defaults={"user": self.user, "prompt": run.prompt, "answer": answer,
                                       "tools": sorted(set(self.used_tools))})
