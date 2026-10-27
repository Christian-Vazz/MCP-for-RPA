"""Importar este pacote registra todas as ferramentas em REGISTRY."""
from . import browser, desktop, files, knowledge_tools, sheets, web  # noqa: F401
from .registry import REGISTRY, ToolContext, ToolResult, available_tools, execute, llm_tool_defs, render_for_llm  # noqa: F401
