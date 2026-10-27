import io
import json

import pandas as pd
from django.test import override_settings

from apps.agent.tools import ToolContext, execute
from apps.workspace import sandbox

from .test_core import ORBIT, Base, tearDownModule  # noqa: F401  (limpa a pasta temporária)

CSV_BR = "produto;cidade;preco;qtd\nCaneta;SP;2,50;10\nCaderno;RJ;15,00;3\nLápis;SP;1,20;40\n"


@override_settings(ORBIT={**ORBIT, "MAX_EDITS_PER_FILE_DAY": 50, "MAX_WRITES_PER_RUN": 15, "WORKSPACE_QUOTA_MB": 20})
class SheetToolsTests(Base):
    def run_tool(self, tool_name, **args):
        return execute(tool_name, args, ToolContext(user=self.user, source="direct"))

    def ok(self, tool_name, **args):
        res = self.run_tool(tool_name, **args)
        self.assertTrue(res.ok, res.data)
        return res.data

    def frame(self, path, sheet=0):
        raw = sandbox.read_bytes(self.user, path)
        if path.endswith(".xlsx"):
            return pd.read_excel(io.BytesIO(raw), sheet_name=sheet)
        return pd.read_csv(io.StringIO(raw.decode()), sep=";", decimal=",")

    def setUp(self):
        super().setUp()
        sandbox.write_text(self.user, "vendas.csv", CSV_BR)

    def test_info_detects_brazilian_csv(self):
        info = self.ok("sheet_info", path="vendas.csv")
        self.assertEqual(info["rows"], 3)
        cols = {c["name"]: c["dtype"] for c in info["columns"]}
        self.assertTrue(cols["preco"].startswith("float"))  # "2,50" virou número
        self.assertAlmostEqual(info["numeric_stats"]["qtd"]["sum"], 53)

    def test_query_filter_sort_and_group(self):
        q = self.ok("sheet_query", path="vendas.csv", filter="cidade == 'SP'", sort_by="preco", descending=True)
        self.assertEqual([r["produto"] for r in q["rows"]], ["Caneta", "Lápis"])
        g = self.ok("sheet_query", path="vendas.csv", group_by="cidade", aggregate="qtd:sum,preco:mean",
                    columns="cidade,qtd")  # uma agregação por coluna: mantém o nome original
        sp = next(r for r in g["rows"] if r["cidade"] == "SP")
        self.assertEqual(sp, {"cidade": "SP", "qtd": 50})
        g2 = self.ok("sheet_query", path="vendas.csv", group_by="cidade", aggregate="qtd:sum,qtd:max")
        self.assertEqual(g2["columns"], ["cidade", "qtd_sum", "qtd_max"])
        missing = self.run_tool("sheet_query", path="vendas.csv", columns="nada")
        self.assertFalse(missing.ok)
        self.assertIn("inexistente", missing.data["error"])

    def test_update_with_formula_add_column_and_keep_csv_format(self):
        self.ok("sheet_add_column", path="vendas.csv", name="total", expression="preco * qtd")
        out = self.ok("sheet_update", path="vendas.csv", filter="produto == 'Caderno'", values='{"qtd": 5}')
        self.assertEqual(out["updated_rows"], 1)
        self.ok("sheet_update", path="vendas.csv", all_rows=True, values={"total": "=preco * qtd"})
        df = self.frame("vendas.csv")
        self.assertEqual(df.loc[df.produto == "Caderno", "total"].item(), 75.0)
        self.assertIn("2,5", sandbox.read_any(self.user, "vendas.csv"))  # mantém separador ";" e decimal ","
        self.assertTrue(sandbox.can_undo(self.user, "vendas.csv"))

    def test_update_requires_filter_or_all_rows(self):
        self.assertFalse(self.run_tool("sheet_update", path="vendas.csv", values='{"qtd": 0}').ok)

    def test_add_and_delete_rows(self):
        self.ok("sheet_add_rows", path="vendas.csv", rows=json.dumps([{"produto": "Borracha", "cidade": "MG",
                                                                      "preco": 3, "qtd": 7}]))
        bad = self.run_tool("sheet_add_rows", path="vendas.csv", rows='[{"produtoo": "x"}]')
        self.assertIn("inexistente", bad.data["error"])
        out = self.ok("sheet_delete_rows", path="vendas.csv", filter="qtd < 5")
        self.assertEqual((out["deleted_rows"], out["rows"]), (1, 3))
        self.assertNotIn("Caderno", sandbox.read_any(self.user, "vendas.csv"))

    def test_edit_columns(self):
        self.ok("sheet_edit_columns", path="vendas.csv", rename='{"qtd": "quantidade"}', drop="cidade")
        self.assertEqual(list(self.frame("vendas.csv").columns), ["produto", "preco", "quantidade"])

    def test_xlsx_create_save_as_sheet_and_edit_preserves_other_sheets(self):
        self.ok("sheet_save_as", path="vendas.csv", destination="relatorio.xlsx", destination_sheet="dados")
        self.ok("sheet_save_as", path="vendas.csv", destination="relatorio.xlsx", destination_sheet="resumo",
                group_by="cidade", aggregate="qtd:sum")
        info = self.ok("sheet_info", path="relatorio.xlsx", sheet="resumo")
        self.assertEqual([s["name"] for s in info["sheets"]], ["dados", "resumo"])
        self.ok("sheet_update", path="relatorio.xlsx", sheet="dados", filter="produto == 'Lápis'", values='{"qtd": 1}')
        self.assertEqual(self.frame("relatorio.xlsx", "dados").qtd.tolist(), [10, 3, 1])
        self.assertEqual(len(self.frame("relatorio.xlsx", "resumo")), 2)  # a outra aba continua lá
        self.ok("undo_edit", path="relatorio.xlsx")
        self.assertEqual(self.frame("relatorio.xlsx", "dados").qtd.tolist(), [10, 3, 40])
        exists = self.run_tool("sheet_save_as", path="vendas.csv", destination="relatorio.xlsx")
        self.assertIn("já existe", exists.data["error"])

    def test_create(self):
        self.ok("sheet_create", path="novo/contatos.xlsx", columns="nome,email", rows='[{"nome": "Ana"}]')
        self.assertEqual(self.frame("novo/contatos.xlsx").nome.tolist(), ["Ana"])
        self.assertFalse(self.run_tool("sheet_create", path="novo/contatos.xlsx", columns="x").ok)
        self.assertFalse(self.run_tool("sheet_create", path="a.txt", columns="x").ok)

    def test_code_injection_in_expressions_is_blocked(self):
        before = sandbox.read_any(self.user, "vendas.csv")
        attacks = ["@pd.io.common.os.system('echo x')", "produto.__class__", "open('x')",
                   "produto.to_csv('fora.csv')", "__import__('os')", "(lambda: 1)()", "[x for x in produto]"]
        for expr in attacks:
            res = self.run_tool("sheet_query", path="vendas.csv", filter=expr)
            self.assertFalse(res.ok, expr)
            res = self.run_tool("sheet_add_column", path="vendas.csv", name="z", expression=expr)
            self.assertFalse(res.ok, expr)
        self.assertEqual(sandbox.read_any(self.user, "vendas.csv"), before)
        # métodos de texto permitidos continuam funcionando
        q = self.ok("sheet_query", path="vendas.csv", filter="produto.str.startswith('Ca') and qtd > 5")
        self.assertEqual(q["rows_total"], 1)

    def test_sheets_stay_inside_workspace(self):
        for bad in ("../x.csv", ".versions/vendas.csv"):
            self.assertFalse(self.run_tool("sheet_info", path=bad).ok)
        self.assertFalse(self.run_tool("sheet_save_as", path="vendas.csv", destination="../fora.csv").ok)
