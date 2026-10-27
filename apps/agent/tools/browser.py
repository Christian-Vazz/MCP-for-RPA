"""Navegador controlado por Selenium. O modelo NÃO recebe screenshots a cada passo: `browser_elements`
devolve uma lista numerada de elementos interativos (barato e confiável) e o modelo age por número.

Clique/digitação/seleção recebem UMA forma de localizar por vez: `by` (lista fechada: numero, id, xpath, css,
class, name, text, ...) + `selector` com o valor correspondente, e `index` para escolher entre vários resultados.
`by="numero"` usa o número de browser_elements; as demais servem para RPA com seletor conhecido."""
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
  const item = {id: n, tag: el.tagName.toLowerCase(), type: el.type || '', text: label, href: (el.href || '').slice(0, 120)};
  if (el.id) item.html_id = el.id;  // permite ao modelo usar by="id" depois
  if (el.name) item.name = el.name;
  const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\\s+/).filter(Boolean).slice(0, 3);
  if (cls.length) item.classes = cls.join(' ');
  out.push(item);
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


# ----------------------------------------------------------------- localização de elementos
# ordem = ordem da lista na aba Ferramentas; "numero" (padrão) é o id gerado por browser_elements
LOCATORS = ("numero", "id", "xpath", "css", "class", "name", "text", "link_text", "partial_link_text",
            "placeholder", "aria_label", "title", "value", "tag", "auto")


def _xpath_literal(text: str) -> str:
    """Literal XPath seguro para qualquer texto (XPath 1.0 não tem escape de aspas: usa concat())."""
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    return "concat(" + ", '\"', ".join(f'"{part}"' for part in text.split('"')) + ")"


def _css_attr(attr: str, value: str) -> str:
    return f'[{attr}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"]'


def _strategy(by: str, selector: str) -> list[tuple[str, str]]:
    """Traduz (by, selector) em uma ou mais estratégias do Selenium, na ordem em que serão tentadas."""
    from selenium.webdriver.common.by import By
    by = (by or "numero").strip().lower().replace("-", "_").replace(" ", "_")
    by = {"class_name": "class", "css_selector": "css", "label": "aria_label", "texto": "text",
          "número": "numero", "element_id": "numero"}.get(by, by)
    sel = str(selector if selector is not None else "").strip()
    if not sel:
        raise ToolError("Informe 'selector' com o valor da forma escolhida em 'by'.")
    if by not in LOCATORS:
        raise ToolError(f"'by' inválido: {by}. Use: {', '.join(LOCATORS)}")
    if by == "numero":
        if not sel.isdigit():
            raise ToolError("Com by='numero', 'selector' deve ser o número do elemento (de browser_elements).")
        return [(By.CSS_SELECTOR, f'[data-orbit-id="{int(sel)}"]')]
    if by == "auto":
        if sel.startswith(("/", "(", "./")):
            return _strategy("xpath", sel)
        if sel[0] in "#.[" or any(c in sel for c in "[]>:~"):
            return _strategy("css", sel)
        return _strategy("id", sel) + _strategy("name", sel) + _strategy("text", sel)
    lit = _xpath_literal(sel)
    return {
        "id": [(By.ID, sel)],
        "css": [(By.CSS_SELECTOR, sel)],
        "xpath": [(By.XPATH, sel)],
        # className do Selenium aceita uma classe só; "btn primary" vira o seletor CSS ".btn.primary"
        "class": [(By.CSS_SELECTOR, "." + ".".join(sel.lstrip(".").split()))] if " " in sel.strip()
        else [(By.CLASS_NAME, sel.lstrip("."))],
        "name": [(By.NAME, sel)],
        "tag": [(By.TAG_NAME, sel)],
        "link_text": [(By.LINK_TEXT, sel)],
        "partial_link_text": [(By.PARTIAL_LINK_TEXT, sel)],
        "placeholder": [(By.CSS_SELECTOR, _css_attr("placeholder", sel))],
        "aria_label": [(By.CSS_SELECTOR, _css_attr("aria-label", sel))],
        "title": [(By.CSS_SELECTOR, _css_attr("title", sel))],
        "value": [(By.CSS_SELECTOR, _css_attr("value", sel))],
        # o elemento MAIS INTERNO cujo texto visível contém o trecho (evita devolver <body> ou <div> pai),
        # mais botões/inputs cujo value seja o texto
        "text": [(By.XPATH, f"//*[contains(normalize-space(.), {lit}) and not(self::script or self::style) "
                            f"and not(.//*[contains(normalize-space(.), {lit})])] | //input[@value={lit}]")],
    }[by]


def _locate(driver, by: str = "numero", selector: str = "", index: int = 0, need_visible: bool = True):
    """Devolve UM elemento localizado pela forma escolhida em `by` com o valor `selector`."""
    found = _find_all(driver, by, selector)
    if need_visible:
        visible = [e for e in found if _visible(e)]
        found = visible or found  # se nenhum estiver visível, ainda tenta (ex.: input escondido por CSS custom)
    if not found:
        if (by or "numero") == "numero":
            raise ToolError("Elemento não encontrado. Chame browser_elements de novo (a página pode ter mudado).")
        raise ToolError(f"Nenhum elemento encontrado com {by}={selector!r}. "
                        "Confira com browser_find ou browser_elements.")
    index = int(index or 0)
    if not 0 <= index < len(found):
        raise ToolError(f"index={index} fora do intervalo: {len(found)} elemento(s) encontrado(s).")
    return found[index]


def _find_all(driver, by, selector):
    from selenium.common.exceptions import InvalidSelectorException, WebDriverException
    for how, what in _strategy(by, selector):
        try:
            els = driver.find_elements(how, what)
        except InvalidSelectorException:
            raise ToolError(f"Seletor inválido para {how}: {what[:120]}")
        except WebDriverException as exc:
            raise ToolError(f"Falha ao procurar o elemento: {str(exc).splitlines()[0][:160]}")
        if els:
            return els
    return []


def _visible(el) -> bool:
    try:
        return el.is_displayed()
    except Exception:  # noqa: BLE001  (elemento "stale")
        return False


def _describe(el) -> dict:
    get = el.get_attribute
    text = " ".join((el.text or get("value") or get("placeholder") or get("aria-label") or "").split())[:80]
    out = {"tag": el.tag_name, "text": text, "visible": _visible(el)}
    for attr in ("id", "name", "type", "class", "href"):
        val = get(attr)
        if val:
            out["html_id" if attr == "id" else attr] = val[:120]
    return out


TARGET_PARAMS = {
    "by": ("string", "UMA forma de localizar: numero (de browser_elements), id, xpath, css, class, name, "
                     "text (texto visível), link_text, partial_link_text, placeholder, aria_label, title, value, tag "
                     "ou auto (deduz pelo formato)", LOCATORS),
    "selector": ("string", "Valor para a forma escolhida, ex.: numero → 3 · id → btnEntrar · "
                           "xpath → //button[@type='submit'] · class → btn primary · text → Entrar"),
    "index": ("integer", "Qual resultado usar se houver vários (0 = primeiro visível)"),
}
FIND_BY = ("string", TARGET_PARAMS["by"][1], LOCATORS)


_on = lambda: cfg("ENABLE_BROWSER")  # noqa: E731


@tool(category="browser", untrusted=True, enabled=_on, required=["url"], params={"url": ("string", "URL https:// a abrir")})
def browser_open(ctx, url: str):
    """Abre uma página HTTPS no navegador controlado e devolve título e URL final."""
    d = _driver(ctx.user)
    d.get(check_url(url))
    return {"title": d.title, "url": d.current_url}


@tool(category="browser", read_only=True, untrusted=True, enabled=_on, params={})
def browser_elements(ctx):
    """Lista os elementos clicáveis/digitáveis visíveis na página, cada um com um número (id). Para agir sobre um
    deles use by="numero" e selector=<id>; html_id, name e classes permitem usar by="id", "name" ou "class"."""
    d = _driver(ctx.user)
    return {"url": d.current_url, "title": d.title, "elements": d.execute_script(_ELEMENTS_JS)}


@tool(category="browser", read_only=True, untrusted=True, enabled=_on, params={"max_chars": ("integer", "Padrão 4000")})
def browser_text(ctx, max_chars: int = 4000):
    """Lê o texto visível da página atual."""
    d = _driver(ctx.user)
    text = d.execute_script("return document.body ? document.body.innerText : ''") or ""
    return {"url": d.current_url, "text": text[: max(500, min(int(max_chars), 15000))]}


@tool(category="browser", read_only=True, untrusted=True, enabled=_on, required=["by", "selector"], params={
    "by": FIND_BY, "selector": TARGET_PARAMS["selector"],
    "limit": ("integer", "Máximo de elementos descritos (padrão 10)")})
def browser_find(ctx, by: str, selector: str, limit: int = 10):
    """Procura elementos por id, xpath, css, class, name, texto etc. e descreve cada um (tag, texto, id, classes,
    visível). Use para conferir um seletor antes de clicar ou digitar."""
    d = _driver(ctx.user)
    els = _find_all(d, by, selector)
    return {"count": len(els), "elements": [{"index": i, **_describe(e)} for i, e in enumerate(els[:max(1, min(int(limit), 30))])]}


@tool(category="browser", risk="medium", untrusted=True, enabled=_on, required=["by", "selector"], params={
    **TARGET_PARAMS, "double": ("boolean", "Clique duplo")})
def browser_click(ctx, by: str, selector: str, index: int = 0, double: bool = False):
    """Clica em um elemento localizado por UMA forma (by) e seu valor (selector): numero de browser_elements,
    id, xpath, css, class, name, texto visível... Pode enviar formulários: exige aprovação (ou aprovação automática)."""
    d = _driver(ctx.user)
    el = _locate(d, by, selector, index)
    target = _describe(el)
    try:
        if double:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(d).double_click(el).perform()
        else:
            el.click()
    except Exception:  # noqa: BLE001  (coberto por outro elemento, fora da tela...): clique via JavaScript
        d.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click()", el)
    time.sleep(0.6)
    return {"clicked": target, "url": d.current_url, "title": d.title}


@tool(category="browser", risk="medium", untrusted=True, enabled=_on, required=["by", "selector", "text"], params={
    **TARGET_PARAMS, "text": ("string", "Texto a digitar"),
    "submit": ("boolean", "Pressionar Enter ao final"),
    "clear": ("boolean", "Limpar o campo antes (padrão: sim)")})
def browser_type(ctx, by: str, selector: str, text: str, index: int = 0, submit: bool = False, clear: bool = True):
    """Digita em um campo localizado por UMA forma (by: numero, id, name, placeholder, xpath...) e seu valor
    (selector). Campos de SENHA são bloqueados: peça ao usuário para preencher manualmente."""
    from selenium.webdriver.common.keys import Keys
    d = _driver(ctx.user)
    el = _locate(d, by, selector, index)
    if (el.get_attribute("type") or "").lower() == "password":
        raise ToolError("Por segurança, não digito em campos de senha. Peça ao usuário para preenchê-lo.")
    if clear:
        el.clear()
    el.send_keys(text + (Keys.ENTER if submit else ""))
    time.sleep(0.4)
    return {"typed": len(text), "field": _describe(el), "url": d.current_url}


@tool(category="browser", risk="medium", untrusted=True, enabled=_on, required=["by", "selector", "option"], params={
    **TARGET_PARAMS, "option": ("string", "Opção a escolher (texto visível ou value)")})
def browser_select(ctx, by: str, selector: str, option: str, index: int = 0):
    """Escolhe uma opção em uma lista suspensa (<select>) localizada por UMA forma (by) e seu valor (selector)."""
    from selenium.common.exceptions import NoSuchElementException, UnexpectedTagNameException
    from selenium.webdriver.support.ui import Select
    d = _driver(ctx.user)
    el = _locate(d, by, selector, index)
    try:
        sel = Select(el)
    except UnexpectedTagNameException:
        raise ToolError("O elemento não é uma lista <select>. Para listas customizadas, use browser_click.")
    try:
        sel.select_by_visible_text(option)
    except NoSuchElementException:
        try:
            sel.select_by_value(option)
        except NoSuchElementException:
            opts = [o.text.strip() for o in sel.options][:20]
            raise ToolError(f"Opção '{option}' não existe. Opções: {', '.join(opts)}")
    return {"selected": sel.first_selected_option.text.strip(), "url": d.current_url}


@tool(category="browser", read_only=True, untrusted=True, enabled=_on, required=["by", "selector"], params={
    "by": FIND_BY, "selector": TARGET_PARAMS["selector"],
    "timeout": ("integer", "Segundos de espera (padrão 10, máx. 30)"),
    "state": ("string", "visible (padrão) | present | hidden", ("visible", "present", "hidden"))})
def browser_wait(ctx, by: str, selector: str, timeout: int = 10, state: str = "visible"):
    """Espera um elemento aparecer (ou sumir) na página. Use após cliques que carregam conteúdo."""
    d = _driver(ctx.user)
    state = (state or "visible").lower()
    deadline = time.monotonic() + max(1, min(int(timeout), 30))
    while True:
        els = _find_all(d, by, selector)
        vis = [e for e in els if _visible(e)]
        done = {"present": bool(els), "hidden": not vis}.get(state, bool(vis))
        if done:
            return {"ok": True, "state": state, "count": len(els),
                    "element": _describe((vis or els)[0]) if els and state != "hidden" else None}
        if time.monotonic() >= deadline:
            raise ToolError(f"Tempo esgotado: {by}={selector!r} não ficou '{state}' em {timeout}s.")
        time.sleep(0.4)


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
