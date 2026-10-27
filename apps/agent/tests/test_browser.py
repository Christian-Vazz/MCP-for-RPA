"""Localização de elementos do navegador (id, xpath, css, class, texto...) com um driver Selenium falso."""
from unittest import mock

from selenium.webdriver.common.by import By

from apps.agent.tools import REGISTRY, ToolContext, execute
from apps.agent.tools import browser as br
from apps.errors import UserFacingError

from .test_core import Base, tearDownModule  # noqa: F401  (limpa a pasta temporária)


class FakeEl:
    def __init__(self, tag="button", text="", visible=True, **attrs):
        self.tag_name, self.text, self.visible, self.attrs = tag, text, visible, attrs
        self.clicked = 0
        self.typed = ""

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_displayed(self):
        return self.visible

    def click(self):
        self.clicked += 1

    def clear(self):
        self.typed = ""

    def send_keys(self, text):
        self.typed += text


class FakeDriver:
    current_url, title = "https://exemplo.com/", "Exemplo"

    def __init__(self, table):
        self.table, self.queries = table, []

    def find_elements(self, how, what):
        self.queries.append((how, what))
        return self.table.get((how, what), [])

    def execute_script(self, script, *args):
        args[0].click()


class SelectorStrategyTests(Base):
    def test_explicit_locators(self):
        self.assertEqual(br._strategy("id", "btnEntrar"), [(By.ID, "btnEntrar")])
        self.assertEqual(br._strategy("xpath", "//button[1]"), [(By.XPATH, "//button[1]")])
        self.assertEqual(br._strategy("css", "form .ok"), [(By.CSS_SELECTOR, "form .ok")])
        self.assertEqual(br._strategy("class", "btn"), [(By.CLASS_NAME, "btn")])
        self.assertEqual(br._strategy("class", "btn primary"), [(By.CSS_SELECTOR, ".btn.primary")])  # várias classes
        self.assertEqual(br._strategy("name", "email"), [(By.NAME, "email")])
        self.assertEqual(br._strategy("CSS-Selector", "a"), [(By.CSS_SELECTOR, "a")])  # sinônimos e caixa
        self.assertEqual(br._strategy("placeholder", 'Seu "e-mail"'), [(By.CSS_SELECTOR, '[placeholder="Seu \\"e-mail\\""]')])
        self.assertEqual(br._strategy("link_text", "Sair"), [(By.LINK_TEXT, "Sair")])

    def test_text_uses_innermost_element_and_safe_literal(self):
        (how, xp), = br._strategy("text", "Entrar")
        self.assertEqual(how, By.XPATH)
        self.assertIn('contains(normalize-space(.), "Entrar")', xp)
        self.assertIn("not(.//*[", xp)  # o elemento mais interno, não o <body>
        self.assertEqual(br._xpath_literal("d'água"), '"d\'água"')
        self.assertEqual(br._xpath_literal('a"b\'c'), "concat(\"a\", '\"', \"b'c\")")

    def test_auto_detection(self):
        self.assertEqual(br._strategy("auto", "//a[@id='x']")[0][0], By.XPATH)
        self.assertEqual(br._strategy("auto", "#login")[0], (By.CSS_SELECTOR, "#login"))
        self.assertEqual(br._strategy("auto", "input[name=q]")[0][0], By.CSS_SELECTOR)
        self.assertEqual([h for h, _ in br._strategy("auto", "entrar")], [By.ID, By.NAME, By.XPATH])  # id → name → texto

    def test_invalid(self):
        with self.assertRaises(UserFacingError):
            br._strategy("magia", "x")
        with self.assertRaises(UserFacingError):
            br._strategy("id", "  ")


class BrowserToolsTests(Base):
    def setUp(self):
        super().setUp()
        self.hidden = FakeEl(text="Entrar", visible=False, id="b0")
        self.first = FakeEl(text="Entrar", id="b1", **{"class": "btn primary"})
        self.second = FakeEl(text="Entrar", id="b2")
        self.pwd = FakeEl(tag="input", type="password", name="senha")
        self.email = FakeEl(tag="input", type="email", name="email")
        self.numbered = FakeEl(text="Menu")
        self.drv = FakeDriver({
            (By.ID, "b1"): [self.first],
            (By.CLASS_NAME, "btn"): [self.hidden, self.first, self.second],
            (By.XPATH, "//input[@type='password']"): [self.pwd],
            (By.NAME, "email"): [self.email],
            (By.CSS_SELECTOR, '[data-orbit-id="7"]'): [self.numbered],
            (By.TAG_NAME, "div"): [FakeEl(tag="div", text="caixa")],
        })
        patcher = mock.patch.object(br, "_driver", return_value=self.drv)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, tool_name, **args):
        return execute(tool_name, args, ToolContext(user=self.user, source="direct"))

    def test_click_by_id_class_index_and_number(self):
        res = self.run_tool("browser_click", by="id", selector="b1")
        self.assertTrue(res.ok, res.data)
        self.assertEqual((self.first.clicked, res.data["clicked"]["html_id"]), (1, "b1"))
        # por classe: ignora o elemento invisível; index escolhe entre os visíveis
        self.run_tool("browser_click", by="class", selector="btn", index=1)
        self.assertEqual((self.hidden.clicked, self.second.clicked), (0, 1))
        self.run_tool("browser_click", by="numero", selector="7")
        self.assertEqual(self.numbered.clicked, 1)

    def test_errors_are_readable(self):
        self.assertIn("Nenhum elemento", self.run_tool("browser_click", by="id", selector="nao-existe").data["error"])
        self.assertIn("fora do intervalo", self.run_tool("browser_click", by="class", selector="btn", index=5).data["error"])
        self.assertIn("obrigatórios", self.run_tool("browser_click").data["error"])
        # uma forma por vez, de uma lista fechada
        self.assertIn("deve ser um de", self.run_tool("browser_click", by="magia", selector="x").data["error"])
        self.assertIn("número do elemento", self.run_tool("browser_click", by="numero", selector="abc").data["error"])
        self.assertIn("browser_elements de novo", self.run_tool("browser_click", by="numero", selector="99").data["error"])

    def test_type_by_name_and_password_still_blocked(self):
        res = self.run_tool("browser_type", by="name", selector="email", text="ana@x.com")
        self.assertTrue(res.ok, res.data)
        self.assertEqual(self.email.typed, "ana@x.com")
        res = self.run_tool("browser_type", by="xpath", selector="//input[@type='password']", text="123")
        self.assertIn("senha", res.data["error"])
        self.assertEqual(self.pwd.typed, "")

    def test_find_wait_and_select(self):
        found = self.run_tool("browser_find", by="class", selector="btn").data
        self.assertEqual(found["count"], 3)
        self.assertEqual([e["visible"] for e in found["elements"]], [False, True, True])
        self.assertTrue(self.run_tool("browser_wait", by="id", selector="b1", timeout=1).data["ok"])
        res = self.run_tool("browser_select", by="tag", selector="div", option="x")
        self.assertIn("não é uma lista", res.data["error"])

    def test_locator_is_a_single_choice_in_the_schema(self):
        for name in ("browser_click", "browser_type", "browser_select", "browser_find", "browser_wait"):
            props = REGISTRY[name].schema()["properties"]
            self.assertEqual(props["by"]["enum"][0], "numero", name)
            self.assertIn("xpath", props["by"]["enum"])
            self.assertNotIn("element_id", props, name)
            self.assertIn("by", REGISTRY[name].required)
        self.assertTrue(self.run_tool("browser_click", by="ID", selector="b1").ok)  # aceita maiúsculas

    def test_risk_levels(self):
        for name in ("browser_click", "browser_type", "browser_select"):
            self.assertEqual(REGISTRY[name].risk, "medium", name)
        for name in ("browser_find", "browser_wait"):
            self.assertEqual((REGISTRY[name].risk, REGISTRY[name].read_only), ("low", True), name)
