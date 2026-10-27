#!/usr/bin/env python
"""Servidor MCP do Orbit Agent (transporte stdio). Escrito para o SDK `mcp` 2.x (API por callbacks).

Expõe as MESMAS ferramentas do agente (apps/agent/tools) a qualquer cliente MCP: Claude Desktop,
Claude Code, etc. Cada chamada roda dentro do workspace e dos limites de UM usuário do sistema.

Uso:
    python mcp_server.py --user voce@email.com [--allow-high-risk]

Exemplo (Claude Code):
    claude mcp add orbit -- python /caminho/orbit_agent/mcp_server.py --user voce@email.com

Exemplo (claude_desktop_config.json):
    {"mcpServers": {"orbit": {"command": "python",
        "args": ["/caminho/orbit_agent/mcp_server.py", "--user", "voce@email.com"]}}}

Ferramentas de risco ALTO (excluir arquivo, controle do desktop) ficam ocultas, a menos que
--allow-high-risk seja passado; nesse caso, a aprovação passa a ser a do cliente MCP.
"""
import argparse
import asyncio
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import mcp.types as types  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import close_old_connections  # noqa: E402
from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

from apps.agent.tools import REGISTRY, ToolContext, available_tools, execute, render_for_llm  # noqa: E402

STATE = {"user": None, "allow_high": False}


def _visible(tool) -> bool:
    return tool.is_enabled() and (tool.risk != "high" or STATE["allow_high"])


async def on_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=t.name, description=t.description or t.name, input_schema=t.schema())
        for t in available_tools() if _visible(t)])


def _call(name: str, arguments: dict):
    """Roda em thread: o ORM do Django é síncrono."""
    close_old_connections()
    try:
        tool = REGISTRY.get(name)
        if tool is None or not _visible(tool):
            return False, f"Ferramenta indisponível: {name}"
        res = execute(name, arguments or {}, ToolContext(user=STATE["user"], source="mcp"))
        return res.ok, render_for_llm(tool, res)
    finally:
        close_old_connections()


async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    ok, text = await asyncio.to_thread(_call, params.name, params.arguments or {})
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=not ok)


server = Server("orbit-agent", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Servidor MCP do Orbit Agent")
    ap.add_argument("--user", required=True, help="E-mail do usuário cujo workspace será exposto")
    ap.add_argument("--allow-high-risk", action="store_true")
    args = ap.parse_args()
    user = get_user_model().objects.filter(username__iexact=args.user).first()
    if not user:
        sys.exit(f"Usuário não encontrado: {args.user}")
    STATE.update(user=user, allow_high=args.allow_high_risk)
    asyncio.run(main())
