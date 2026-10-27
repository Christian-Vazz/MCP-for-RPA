"""Exercita o caminho de produção (OllamaLLM) sem rede: um requests.post falso devolve streams NDJSON
no formato real de /api/chat. Valida o payload enviado, o parse do stream e o loop completo do agente."""
import json
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.accounts.models import Profile
from apps.agent.models import Conversation, Message, Run
from apps.agent.services import llm as llm_mod
from apps.agent.services import runner
from apps.workspace import sandbox

User = get_user_model()
ORBIT = {**settings.ORBIT, "LLM_PROVIDER": "auto", "OLLAMA_API_KEY": "ok-test", "OLLAMA_BASE_URL": "",
         "MODEL": "gpt-oss:120b", "OLLAMA_THINK": "medium", "SYNC_RUNS": True,
         "VECTOR_STORE": "local", "EMBEDDING_BACKEND": "hash"}


def ndjson(*chunks):
    return [json.dumps(c).encode() for c in chunks]


def chunk(**message):
    return {"model": "gpt-oss:120b", "message": {"role": "assistant", "content": "", **message}, "done": False}


def done(reason, prompt_tokens, out_tokens):
    return {"model": "gpt-oss:120b", "message": {"role": "assistant", "content": ""}, "done": True,
            "done_reason": reason, "prompt_eval_count": prompt_tokens, "eval_count": out_tokens}


TOOL_TURN = ndjson(
    chunk(thinking="Preciso listar "), chunk(thinking="os arquivos."),
    chunk(content="Vou listar "), chunk(content="os arquivos."),
    chunk(tool_calls=[{"function": {"name": "list_files", "arguments": {}}}]),
    done("stop", 1200, 40))

FINAL_TURN = ndjson(chunk(content="Você tem 1 arquivo: notas.txt."), done("stop", 1300, 15))


class FakeResponse:
    def __init__(self, lines, status=200):
        self.lines, self.status_code = lines, status

    def iter_lines(self):
        return iter(self.lines)

    def json(self):
        return {"error": "model 'gpt-oss:120b' not found"}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(ORBIT=ORBIT, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class OllamaWireTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.mkdtemp()
        ms = self.settings(MEDIA_ROOT=self.tmp)
        ms.enable()
        self.addCleanup(ms.disable)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.user = User.objects.create_user("w@x.com", "w@x.com", "senha-forte-123")
        Profile.objects.create(user=self.user)
        sandbox.write_text(self.user, "notas.txt", "olá")
        self.calls, self.turns = [], [TOOL_TURN, FINAL_TURN]

        def fake_post(url, json=None, headers=None, **kw):
            self.calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse(self.turns.pop(0))

        patcher = mock.patch.object(llm_mod.requests, "post", side_effect=fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)
        llm_mod._llm = None
        self.addCleanup(setattr, llm_mod, "_llm", None)

    def _run(self, prompt="liste meus arquivos"):
        conv = Conversation.objects.create(user=self.user, title="t")
        msg = Message.objects.create(conversation=conv, role="user", content=prompt)
        run = Run.objects.create(user=self.user, conversation=conv, user_message=msg, prompt=msg.content)
        runner.submit(run.pk)
        run.refresh_from_db()
        return run

    def test_full_agent_loop_over_ollama_wire_format(self):
        self.assertEqual(llm_mod.provider_name(), "ollama")  # auto + chave => Ollama
        run = self._run()

        self.assertEqual(run.status, "done", run.error)
        self.assertIn("notas.txt", run.answer)
        self.assertNotIn("Preciso listar", run.answer)  # o raciocínio não vai para a resposta
        self.assertEqual(run.steps, 2)
        self.assertEqual((run.input_tokens, run.output_tokens), (2500, 55))

        first, second = self.calls
        self.assertEqual(first["url"], "https://ollama.com/api/chat")  # chave sem URL => Ollama Cloud
        self.assertEqual(first["headers"]["Authorization"], "Bearer ok-test")
        body = first["json"]
        self.assertTrue(body["stream"])
        self.assertEqual((body["model"], body["think"]), ("gpt-oss:120b", "medium"))
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("notas.txt", body["messages"][0]["content"])  # workspace entra no contexto dinâmico
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "liste meus arquivos"})
        names = {t["function"]["name"] for t in body["tools"]}
        self.assertIn("list_files", names)
        self.assertTrue(all(t["type"] == "function" and "parameters" in t["function"] for t in body["tools"]))
        # 2ª chamada: chamada da ferramenta (com o raciocínio) + resultado marcado como dado não confiável
        assistant, result = second["json"]["messages"][-2:]
        self.assertEqual(assistant["tool_calls"], [{"function": {"name": "list_files", "arguments": {}}}])
        self.assertEqual(assistant["thinking"], "Preciso listar os arquivos.")
        self.assertEqual((result["role"], result["tool_name"]), ("tool", "list_files"))
        self.assertIn("notas.txt", result["content"])
        kinds = list(run.events.values_list("kind", flat=True))
        self.assertEqual(kinds, ["text", "tool_call", "tool_result", "text", "done"])

    def test_missing_model_becomes_readable_error(self):
        llm_mod.requests.post.side_effect = lambda *a, **kw: FakeResponse([], status=404)
        run = self._run("olá")
        self.assertEqual(run.status, "error")
        self.assertIn("ollama pull gpt-oss:120b", run.error)
