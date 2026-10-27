"""Controle do desktop REAL com BotCity (visão computacional por imagem-modelo).

⚠ Desligado por padrão (ENABLE_DESKTOP=0). Move o mouse e o teclado da máquina onde o servidor roda,
portanto só faz sentido em uso local/demonstração. Todas as ferramentas são de risco ALTO: o usuário
aprova cada ação. Imagens-modelo (botões, ícones) ficam em workspace/vision/<nome>.png.
"""
import threading

from apps.conf import cfg
from apps.errors import UserFacingError as ToolError
from apps.workspace import sandbox

from .registry import tool

_bot = None
_lock = threading.Lock()
_on = lambda: cfg("ENABLE_DESKTOP")  # noqa: E731


def _get_bot():
    global _bot
    with _lock:
        if _bot is None:
            try:
                from botcity.core import DesktopBot
            except ImportError:
                raise ToolError("Instale 'botcity-framework-core' para controlar o desktop.")
            _bot = DesktopBot()
        return _bot


@tool(category="desktop", risk="high", enabled=_on, params={"name": ("string", "Nome do arquivo .png (opcional)")})
def desktop_screenshot(ctx, name: str = "tela.png"):
    """Captura a tela inteira do computador e salva no workspace (screenshots/)."""
    bot = _get_bot()
    filename = sandbox.safe_name(name or "tela.png")
    if not filename.lower().endswith(".png"):
        filename += ".png"
    target = sandbox.resolve(ctx.user, f"screenshots/{filename}")
    target.parent.mkdir(parents=True, exist_ok=True)
    bot.screenshot(str(target))
    sandbox.bump(ctx.user)
    ctx.changed = True
    return {"path": f"screenshots/{filename}"}


@tool(category="desktop", risk="high", enabled=_on, required=["image"], params={
    "image": ("string", "Imagem-modelo no workspace, ex.: vision/botao_salvar.png"),
    "matching": ("integer", "Similaridade 50-99 (padrão 90)"),
    "wait_seconds": ("integer", "Tempo máximo procurando (padrão 8)")})
def desktop_click_image(ctx, image: str, matching: int = 90, wait_seconds: int = 8):
    """Procura uma imagem-modelo na tela (visão computacional do BotCity) e clica nela."""
    bot = _get_bot()
    path = sandbox.resolve(ctx.user, image, must_exist=True)
    bot.add_image("orbit_target", str(path))
    found = bot.find("orbit_target", matching=max(0.5, min(matching, 99)) / 100,
                     waiting_time=max(1, min(wait_seconds, 30)) * 1000)
    if not found:
        raise ToolError("Imagem não encontrada na tela.")
    bot.click()
    return {"clicked": True, "image": image}


@tool(category="desktop", risk="high", enabled=_on, required=["x", "y"],
      params={"x": ("integer", "Coordenada X"), "y": ("integer", "Coordenada Y")})
def desktop_click_at(ctx, x: int, y: int):
    """Clica em uma coordenada da tela."""
    _get_bot().click_at(int(x), int(y))
    return {"clicked_at": [x, y]}


@tool(category="desktop", risk="high", enabled=_on, required=["text"], params={"text": ("string", "Texto a digitar")})
def desktop_type(ctx, text: str):
    """Digita um texto na janela em foco (usa a área de transferência para suportar acentos)."""
    if len(text) > 500:
        raise ToolError("Texto longo demais (máx. 500 caracteres).")
    _get_bot().paste(text)
    return {"typed": len(text)}


@tool(category="desktop", risk="high", enabled=_on, required=["keys"], params={
    "keys": ("array", "Teclas do atalho, ex.: ['ctrl','s']")})
def desktop_hotkey(ctx, keys: list):
    """Pressiona um atalho de teclado."""
    from botcity.core import Keys  # noqa: F401  (garante o import do pacote)
    _get_bot().type_keys([str(k).lower() for k in keys][:4])
    return {"pressed": keys}
