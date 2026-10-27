"""Camada de LLM. Interface única: complete(system, messages, tools, on_text) -> LLMResult.

* OllamaLLM: API do Ollama (/api/chat) com streaming, tool calling e raciocínio (gpt-oss). Serve tanto
  o Ollama local (http://localhost:11434) quanto o Ollama Cloud (https://ollama.com, com chave).
* MockLLM: modo demonstração (Ollama não configurado). Permite apresentar/testar todo o sistema offline.

O runner fala um formato interno único (blocos text/tool_use/tool_result); cada provedor traduz esse
formato para o da sua API. Assim o loop do agente não depende do provedor.
"""
import json
import re
import time
import uuid
from dataclasses import dataclass, field

import requests

from apps.conf import cfg


@dataclass
class LLMResult:
    blocks: list
    stop_reason: str
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})


class LLMError(RuntimeError):
    """Falha ao falar com o provedor (mensagem segura para exibir ao usuário)."""


def ollama_url() -> str:
    url = cfg("OLLAMA_BASE_URL") or ("https://ollama.com" if cfg("OLLAMA_API_KEY") else "http://localhost:11434")
    return url.rstrip("/")


class OllamaLLM:
    name = "ollama"
    RETRY_STATUS = (429, 500, 502, 503, 504)

    # ------------------------------------------------------------------ formato interno -> Ollama
    @staticmethod
    def _messages(system, messages) -> list:
        sys_text = system if isinstance(system, str) else "\n\n".join(b["text"] for b in system)
        out = [{"role": "system", "content": sys_text}]
        names = {}  # tool_use id -> nome: o Ollama associa o resultado pelo nome da ferramenta, não por id
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": m["role"], "content": content})
                continue
            if m["role"] == "assistant":
                msg = {"role": "assistant", "content": "".join(b["text"] for b in content if b["type"] == "text")}
                thinking = "".join(b["thinking"] for b in content if b["type"] == "thinking")
                if thinking:  # o gpt-oss precisa do raciocínio anterior para continuar uma cadeia de ferramentas
                    msg["thinking"] = thinking
                calls = []
                for b in content:
                    if b["type"] == "tool_use":
                        names[b["id"]] = b["name"]
                        calls.append({"function": {"name": b["name"], "arguments": b["input"] or {}}})
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
                continue
            for b in content:
                if b["type"] == "tool_result":
                    body = str(b["content"])
                    out.append({"role": "tool", "tool_name": names.get(b["tool_use_id"], ""),
                                "content": ("ERRO: " + body) if b.get("is_error") else body})
                elif b["type"] == "text":
                    out.append({"role": "user", "content": b["text"]})
        return out

    @staticmethod
    def _tools(tools) -> list:
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["input_schema"]}} for t in tools]

    # ------------------------------------------------------------------ HTTP
    def _post(self, payload):
        headers = {"Content-Type": "application/json"}
        if cfg("OLLAMA_API_KEY"):
            headers["Authorization"] = f"Bearer {cfg('OLLAMA_API_KEY')}"
        url = ollama_url()
        for attempt in range(3):  # só repete ANTES do stream começar (nada foi mostrado ao usuário ainda)
            try:
                resp = requests.post(f"{url}/api/chat", json=payload, headers=headers, stream=True,
                                     timeout=(10, cfg("LLM_TIMEOUT")))
            except requests.ConnectionError:
                if attempt == 2:
                    raise LLMError(f"Não foi possível conectar ao Ollama em {url}. Ele está rodando?")
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in self.RETRY_STATUS or attempt == 2:
                    raise LLMError(self._http_error(resp))
                resp.close()
            time.sleep(1 + attempt)

    @staticmethod
    def _http_error(resp) -> str:
        try:
            detail = resp.json().get("error", "")
        except ValueError:
            detail = resp.text[:200]
        if resp.status_code in (401, 403):
            return f"O Ollama recusou a autenticação ({resp.status_code}). Confira OLLAMA_API_KEY."
        if resp.status_code == 404:
            return f"Modelo '{cfg('MODEL')}' não encontrado no Ollama ({detail}). Rode: ollama pull {cfg('MODEL')}"
        return f"O Ollama respondeu {resp.status_code}: {detail}"

    # ------------------------------------------------------------------ chamada
    def complete(self, system, messages, tools, on_text=None) -> LLMResult:
        payload = {"model": cfg("MODEL"), "stream": True, "messages": self._messages(system, messages),
                   "tools": self._tools(tools),
                   "options": {"num_predict": cfg("MAX_OUTPUT_TOKENS"), "num_ctx": cfg("OLLAMA_NUM_CTX")}}
        if cfg("OLLAMA_THINK"):
            payload["think"] = cfg("OLLAMA_THINK")  # gpt-oss aceita low | medium | high
        text, thinking, calls, final = "", "", [], {}
        with self._post(payload) as resp:
            for line in resp.iter_lines():  # NDJSON: um objeto por linha
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise LLMError(f"Ollama: {chunk['error']}")
                msg = chunk.get("message") or {}
                thinking += msg.get("thinking") or ""
                calls.extend(msg.get("tool_calls") or [])
                if msg.get("content"):
                    text += msg["content"]
                    if on_text:
                        on_text(text)
                if chunk.get("done"):
                    final = chunk
                    break
        blocks = []
        if thinking:  # o runner ignora este bloco na resposta; ele só volta para o modelo no próximo passo
            blocks.append({"type": "thinking", "thinking": thinking})
        if text.strip():
            blocks.append({"type": "text", "text": text})
        for c in calls:
            fn = c.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # alguns modelos devolvem os argumentos como texto JSON
                try:
                    args = json.loads(args or "{}")
                except ValueError:
                    args = {}
            blocks.append({"type": "tool_use", "id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                           "name": fn.get("name", ""), "input": args if isinstance(args, dict) else {}})
        if calls:
            stop = "tool_use"
        elif final.get("done_reason") == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        return LLMResult(blocks, stop, {"input": final.get("prompt_eval_count", 0) or 0,
                                        "output": final.get("eval_count", 0) or 0,
                                        "cache_read": 0, "cache_write": 0})


class MockLLM:
    """LLM simulado e determinístico: reconhece alguns padrões e chama ferramentas de verdade."""
    name = "mock"

    def _stream(self, text, on_text):
        if on_text:
            acc = ""
            for i in range(0, len(text), 12):
                acc = text[: i + 12]
                on_text(acc)
                time.sleep(0.02)

    def complete(self, system, messages, tools, on_text=None) -> LLMResult:
        est_in = max(1, len(json.dumps(messages, default=str)) // 4)
        last = messages[-1]
        usage = {"input": est_in, "output": 0, "cache_read": 0, "cache_write": 0}

        if isinstance(last["content"], list):  # acabamos de receber resultados de ferramentas
            results = [b for b in last["content"] if b.get("type") == "tool_result"]
            body = str(results[0]["content"])[:700] if results else ""
            failed = bool(results and results[0].get("is_error"))
            text = ("A ferramenta retornou um erro:\n\n" if failed else "Pronto! Resultado da ferramenta:\n\n") \
                + f"```\n{body}\n```\n\n(Modo demonstração: configure o Ollama no .env para respostas reais.)"
            self._stream(text, on_text)
            usage["output"] = len(text) // 4
            return LLMResult([{"type": "text", "text": text}], "end_turn", usage)

        prompt = str(last["content"])
        low = prompt.lower()
        call = None
        if m := re.search(r"https://\S+", prompt):
            call = ("web_fetch", {"url": m.group(0).rstrip(".,)"), "max_chars": 1500}, "Vou consultar a página.")
        elif m := re.search(r"(?:ler|leia|abra|resuma)\s+(?:o\s+arquivo\s+)?([\w./ -]+\.\w{2,4})", low):
            call = ("read_file", {"path": m.group(1).strip(), "max_chars": 1500}, "Vou ler o arquivo.")
        elif m := re.search(r"(?:apague|exclua|delete)\s+(?:o\s+arquivo\s+)?([\w./ -]+\.\w{2,4})", low):
            call = ("delete_file", {"path": m.group(1).strip()}, "Vou excluir o arquivo (peço sua aprovação).")
        elif any(w in low for w in ("liste", "listar", "arquivos")):
            call = ("list_files", {}, "Vou listar os arquivos do workspace.")
        elif low.startswith(("lembre", "memorize", "guarde")):
            call = ("memory_save", {"content": prompt[:300], "kind": "note"}, "Vou guardar isso na memória.")
        if call:
            name, args, intro = call
            self._stream(intro, on_text)
            usage["output"] = 40
            return LLMResult([{"type": "text", "text": intro},
                              {"type": "tool_use", "id": f"toolu_mock_{int(time.time()*1000)}", "name": name, "input": args}],
                             "tool_use", usage)
        text = ("Estou em **modo demonstração** (Ollama não configurado). Tente: *“liste meus arquivos”*, "
                "*“leia notas.txt”*, *“apague notas.txt”* (pede aprovação), *“consulte https://example.com”* ou *“lembre que prefiro respostas curtas”*.")
        self._stream(text, on_text)
        usage["output"] = len(text) // 4
        return LLMResult([{"type": "text", "text": text}], "end_turn", usage)


_llm = None


def provider_name() -> str:
    p = cfg("LLM_PROVIDER")
    if p == "auto":
        return "ollama" if cfg("OLLAMA_API_KEY") or cfg("OLLAMA_BASE_URL") else "mock"
    return p


def get_llm():
    global _llm
    name = provider_name()
    if _llm is None or _llm.name != name:
        _llm = OllamaLLM() if name == "ollama" else MockLLM()
    return _llm
