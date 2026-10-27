"""Navegador controlado por Selenium. O modelo NÃO recebe screenshots a cada passo: `browser_elements`
devolve uma lista numerada de elementos interativos (barato e confiável) e o modelo age por número."""
import threading
import time
from datetime import datetime

from apps.conf import cfg
from apps.errors import UserFacingError as ToolError
from apps.workspace import sandbox

from .netpolicy import check_url
from .registry import tool

_sessions: dict[int, dict] = {}
_lock = threading.Lock()

_ELEMENTS_JS = """
document.querySelectorAll('[data-orbit-id]').forEach(e => e.removeAttribute('data-orbit-id'));
const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[onclick]';
const out = []; let n = 0;
for (const el of document.querySelectorAll(sel)) {
  const r = el.getBoundingClientRect(), st = getComputedStyle(el);
  if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
  n++; el.setAttribute('data-orbit-id', n);
  const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || el.name || '')
      .trim().replace(/\\s+/g, ' ').slice(0, 80);
  out.push({id: n, tag: el.tagName.toLowerCase(), type: el.type || '', text: label, href: (el.href || '').slice(0, 120)});
  if (n >= 60) break;
}
return out;
"""


def _make_driver():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        raise ToolError("Instale 'selenium' para usar o navegador.")
    opts = Options()
    if cfg("BROWSER_HEADLESS"):
        opts.add_argument("--headless=new")
    if cfg("BROWSER_NO_SANDBOX"):
        opts.add_argument("--no-sandbox")
    for arg in ("--window-size=1280,800", "--disable-gpu", "--disable-dev-shm-usage", "--lang=pt-BR"):
        opts.add_argument(arg)
    opts.page_load_strategy = "eager"
    try:
        driver = webdriver.Chrome(options=opts)  # Selenium Manager baixa o driver automaticamente
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Não foi possível iniciar o Chrome: {str(exc)[:200]}")
    driver.set_page_load_timeout(25)
    return driver


def _driver(user):
    now = time.time()
    with _lock:
        for uid in [u for u, s in _sessions.items() if now - s["last"] > cfg("BROWSER_IDLE_SECONDS")]:
            try:
                _sessions.pop(uid)["driver"].quit()
            except Exception:  # noqa: BLE001
                pass
        sess = _sessions.get(user.pk)
        if sess is None:
            sess = _sessions[user.pk] = {"driver": _make_driver(), "last": now}
        sess["last"] = now
        return sess["driver"]


def close_session(user):
    with _lock:
        sess = _sessions.pop(user.pk, None)
    if sess:
        try:
            sess["driver"].quit()
        except Exception:  # noqa: BLE001
            pass


def _find(driver, element_id: int):
    from selenium.webdriver.common.by import By
    els = driver.find_elements(By.CSS_SELECTOR, f'[data-orbit-id="{int(element_id)}"]')
    if not els:
        raise ToolError("Elemento não encontrado. Chame browser_elements de novo (a página pode ter mudado).")
    return els[0]


_on = lambda: cfg("ENABLE_BROWSER")  # noqa: E731


@tool(category="browser", untrusted=True, enabled=_on, required=["url"], params={"url": ("string", "URL https:// a abrir")})
def browser_open(ctx, url: str):
    """Abre uma página HTTPS no navegador controlado e devolve título e URL final."""
    d = _driver(ctx.user)
    d.get(check_url(url))
    return {"title": d.title, "url": d.current_url}


@tool(category="browser", read_only=True, untrusted=True, enabled=_on, params={})
def browser_elements(ctx):
    """Lista os elementos clicáveis/digitáveis visíveis na página, cada um com um número (id)."""
    d = _driver(ctx.user)
    return {"url": d.current_url, "title": d.title, "elements": d.execute_script(_ELEMENTS_JS)}


@tool(category="browser", read_only=True, untrusted=True, enabled=_on, params={"max_chars": ("integer", "Padrão 4000")})
def browser_text(ctx, max_chars: int = 4000):
    """Lê o texto visível da página atual."""
    d = _driver(ctx.user)
    text = d.execute_script("return document.body ? document.body.innerText : ''") or ""
    return {"url": d.current_url, "text": text[: max(500, min(int(max_chars), 15000))]}


@tool(category="browser", risk="medium", untrusted=True, enabled=_on, required=["element_id"],
      params={"element_id": ("integer", "Número do elemento retornado por browser_elements")})
def browser_click(ctx, element_id: int):
    """Clica em um elemento da página pelo número. Pode enviar formulários: exige aprovação (ou aprovação automática)."""
    d = _driver(ctx.user)
    el = _find(d, element_id)
    try:
        el.click()
    except Exception:  # noqa: BLE001
        d.execute_script("arguments[0].click()", el)
    time.sleep(0.6)
    return {"url": d.current_url, "title": d.title}


@tool(category="browser", risk="medium", untrusted=True, enabled=_on, required=["element_id", "text"], params={
    "element_id": ("integer", "Número do campo"), "text": ("string", "Texto a digitar"),
    "submit": ("boolean", "Pressionar Enter ao final")})
def browser_type(ctx, element_id: int, text: str, submit: bool = False):
    """Digita em um campo. Campos de SENHA são bloqueados: peça ao usuário para preencher manualmente."""
    from selenium.webdriver.common.keys import Keys
    d = _driver(ctx.user)
    el = _find(d, element_id)
    if (el.get_attribute("type") or "").lower() == "password":
        raise ToolError("Por segurança, não digito em campos de senha. Peça ao usuário para preenchê-lo.")
    el.clear()
    el.send_keys(text + (Keys.ENTER if submit else ""))
    time.sleep(0.4)
    return {"typed": len(text), "url": d.current_url}


@tool(category="browser", enabled=_on, params={"name": ("string", "Nome do arquivo .png (opcional)")})
def browser_screenshot(ctx, name: str = ""):
    """Salva uma captura da página no workspace (screenshots/). Útil para relatórios e evidências."""
    d = _driver(ctx.user)
    filename = sandbox.safe_name(name) if name else f"captura_{datetime.now():%Y%m%d_%H%M%S}.png"
    if not filename.lower().endswith(".png"):
        filename += ".png"
    out = sandbox.save_bytes(ctx.user, f"screenshots/{filename}", d.get_screenshot_as_png(), action="write")
    ctx.changed = True
    return out


@tool(category="browser", enabled=_on, params={})
def browser_close(ctx):
    """Fecha o navegador do usuário e libera memória."""
    close_session(ctx.user)
    return {"closed": True}
