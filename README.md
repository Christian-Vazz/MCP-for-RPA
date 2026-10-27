# Orbit Agent

Agente de workspace com interface web: o modelo decide, usa ferramentas (arquivos, web, navegador, memória, RAG, desktop) e responde — com aprovação humana para ações de risco, cache, memória e dashboard.

> Leia o **[CLAUDE.md](CLAUDE.md)** para o comportamento do agente, a arquitetura e as regras do projeto.

## Início rápido

```bash
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # opcional: coloque OLLAMA_API_KEY (Ollama Cloud) ou OLLAMA_BASE_URL (Ollama local)
python manage.py migrate
python manage.py runserver      # abra http://127.0.0.1:8000 e crie sua conta
```

**Sem Ollama configurado** o sistema roda em **modo demonstração** (LLM simulado, mas com ferramentas, cache, memória e aprovações reais). Experimente: `liste meus arquivos` · `leia notas.txt` · `apague notas.txt` (pede aprovação) · `lembre que prefiro respostas curtas`.

## Recursos

- Login por e-mail, cadastro com consentimento de aprendizado, bloqueio após tentativas falhas.
- Chat com passos do agente em tempo real (AJAX), aprovação inline, parar execução, 👍/👎.
- Workspace por usuário: enviar/baixar/prévia, edição pelo agente com limites, **desfazer** e lixeira.
- Aba **Ferramentas**: execute qualquer função diretamente, sem o modelo.
- **Memória** de longo prazo e **RAG** sobre arquivos/sites (com consentimento).
- Tema claro/escuro. Dashboard com uso, cache, tokens economizados, ferramentas e armazenamento vetorial.
- **Servidor MCP** com as mesmas ferramentas (`python mcp_server.py --user voce@email.com`).

## Capacidades opcionais

| Recurso | Como ativar |
|---|---|
| Navegador (Selenium) | Padrão ligado; requer Google Chrome instalado. `BROWSER_HEADLESS=0` para ver o navegador. |
| Desktop (BotCity) | `pip install botcity-framework-core` e `ENABLE_DESKTOP=1`. Controla o mouse/teclado **reais** — só local. |
| Embeddings semânticos | `pip install sentence-transformers`, `EMBEDDING_BACKEND=st`, `python manage.py reindex_knowledge`. |
| Cache compartilhado | `REDIS_URL=redis://localhost:6379/0` (necessário com vários processos). |

## MCP

```bash
claude mcp add orbit -- python /caminho/orbit_agent/mcp_server.py --user voce@email.com
```
Ferramentas de risco alto ficam ocultas, a menos que use `--allow-high-risk`. O servidor foi escrito para o SDK `mcp` **2.x**.

## Testes

```bash
python manage.py test apps
```

## Estado da verificação

Verificado automaticamente neste ambiente: toda a suíte (33 testes), o fluxo completo no navegador (login, chat em tempo real, cache, aprovação, abas, upload, temas claro/escuro, dashboard, mobile), o cliente MCP real via stdio e o caminho da API do Ollama com um stream NDJSON simulado e uma execução real com `gpt-oss:120b` no Ollama Cloud (ler → acrescentar linha → conferir).

**Não foi possível verificar aqui** (exige o seu ambiente): Selenium com Chrome/ChromeDriver real (o JavaScript de listagem de elementos foi validado em página real, mas o *driver* não), BotCity (precisa de interface gráfica) e `sentence-transformers`. Teste esses três com calma antes da apresentação.

## Produção (se for além da demonstração)

`DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` forte, `DJANGO_ALLOWED_HOSTS`, HTTPS, Redis, gunicorn com 1 processo e várias threads, e o navegador do agente em contêiner sem acesso à rede interna.
