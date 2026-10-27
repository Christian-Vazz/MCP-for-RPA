"""Memória ampliada (textos, arquivos, links) e integração com o Pinecone (cliente falso, sem rede)."""
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.accounts.models import Profile
from apps.errors import UserFacingError
from apps.knowledge import embeddings, store, vectorstore
from apps.knowledge.embeddings import HashEmbedder, PineconeEmbedder
from apps.knowledge.models import Chunk, Document, Retrieval
from apps.workspace import sandbox

from .test_core import ORBIT, Base, tearDownModule  # noqa: F401  (limpa a pasta temporária)

User = get_user_model()
LONG = ("O fechamento mensal começa no dia 25. Primeiro exporte o relatório de vendas do ERP, "
        "depois concilie os pagamentos do banco e envie o resumo ao financeiro até o dia 30.")


class MemoryApiTests(Base):
    def test_text_memory_is_listed_searchable_and_deletable(self):
        res = self.post("/api/memories/", {"type": "text", "title": "Fechamento mensal", "content": LONG})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["status"], "indexed")
        docs = self.client.get("/api/memories/").json()["documents"]
        self.assertEqual([(d["type"], d["title"]) for d in docs], [("text", "Fechamento mensal")])
        hits = store.search(self.user, "quando começa o fechamento mensal?", min_score=0.05)
        self.assertTrue(hits and hits[0]["source"] == "Fechamento mensal")
        # mesmo título = atualiza (não duplica)
        self.post("/api/memories/", {"type": "text", "title": "Fechamento mensal", "content": LONG + " Revisado."})
        self.assertEqual(Document.objects.filter(user=self.user).count(), 1)
        self.assertTrue(self.client.delete(f"/api/knowledge/{docs[0]['id']}/").json()["deleted"])
        self.assertEqual(store.search(self.user, "fechamento mensal", min_score=0.0), [])

    def test_short_fact_still_works(self):
        self.assertEqual(self.post("/api/memories/", {"content": "prefiro respostas curtas"}).json()["status"], "created")
        self.assertEqual(len(self.client.get("/api/memories/").json()["memories"]), 1)

    def test_workspace_file_and_upload_become_memory(self):
        sandbox.write_text(self.user, "proc.md", LONG)
        self.assertEqual(self.post("/api/memories/", {"type": "file", "path": "proc.md"}).json()["status"], "indexed")
        self.assertEqual(self.post("/api/memories/", {"type": "file", "path": "../x.md"}).status_code, 400)
        res = self.client.post("/api/memories/upload/", {"files": SimpleUploadedFile("guia.txt", LONG.encode())}).json()
        self.assertEqual(res["saved"][0]["path"], "memoria/guia.txt")
        self.assertEqual({d["source"] for d in res["documents"]}, {"proc.md", "memoria/guia.txt"})
        bad = self.client.post("/api/memories/upload/", {"files": SimpleUploadedFile("x.exe", b"MZ")}).json()
        self.assertTrue(bad["errors"])

    def test_url_memory_uses_network_policy(self):
        with mock.patch("apps.agent.tools.knowledge_tools.check_url", side_effect=lambda u: u), \
             mock.patch("apps.agent.tools.knowledge_tools._download", return_value=("Manual", LONG)):
            res = self.post("/api/memories/", {"type": "url", "url": "https://exemplo.com/manual"}).json()
        self.assertEqual((res["status"], res["title"]), ("indexed", "Manual"))
        blocked = self.post("/api/memories/", {"type": "url", "url": "http://127.0.0.1/admin"})
        self.assertEqual(blocked.status_code, 400)

    def test_agent_tool_memory_add_text(self):
        from apps.agent.tools import ToolContext, execute
        res = execute("memory_add_text", {"title": "Proc", "content": LONG}, ToolContext(user=self.user))
        self.assertTrue(res.ok, res.data)
        self.assertEqual(Chunk.objects.filter(user=self.user).count(), res.data["chunks"])

    def test_cannot_delete_other_users_document(self):
        store.add_text(self.user, "Meu", LONG)
        doc = Document.objects.get(user=self.user)
        other = User.objects.create_user("b@x.com", "b@x.com", "senha-forte-123")
        Profile.objects.create(user=other)
        c = Client()
        c.force_login(other)
        self.assertFalse(c.delete(f"/api/knowledge/{doc.pk}/").json()["deleted"])
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())


# ------------------------------------------------------------------ Pinecone falso
class FakeIndex:
    def __init__(self):
        self.ns: dict[str, dict] = {}
        self.fail_query = False

    def upsert(self, vectors, namespace=""):
        for v in vectors:
            self.ns.setdefault(namespace, {})[v["id"]] = v

    def delete(self, ids, namespace=""):
        for i in ids:
            self.ns.get(namespace, {}).pop(i, None)

    def describe_index_stats(self):
        if self.fail_query:
            raise ConnectionError("pinecone fora do ar")
        return SimpleNamespace(total_vector_count=sum(len(v) for v in self.ns.values()),
                               namespaces={k: SimpleNamespace(vector_count=len(v)) for k, v in self.ns.items()})

    def query(self, vector, top_k, namespace="", include_metadata=False):
        if self.fail_query:
            raise ConnectionError("pinecone fora do ar")
        import numpy as np
        q = np.asarray(vector)
        scored = sorted(((float(np.dot(q, v["values"])), v) for v in self.ns.get(namespace, {}).values()),
                        key=lambda t: -t[0])[:top_k]
        return SimpleNamespace(matches=[SimpleNamespace(id=v["id"], score=s, metadata=v["metadata"]) for s, v in scored])


class FakePinecone:
    def __init__(self, dimension=None):
        self.index_obj, self.created, self.dimension = FakeIndex(), [], dimension
        self.embed_calls = []
        self.inference = SimpleNamespace(embed=self._embed)

    def has_index(self, name):
        return self.dimension is not None

    def create_index(self, name, dimension, metric, spec):
        self.created.append((name, dimension, metric))
        self.dimension = dimension

    def describe_index(self, name):
        return SimpleNamespace(dimension=self.dimension)

    def Index(self, name):  # noqa: N802  (mesmo nome do SDK)
        return self.index_obj

    def _embed(self, model, inputs, parameters):
        self.embed_calls.append((model, len(inputs), parameters["input_type"]))
        vecs = HashEmbedder(1024).embed(inputs) * 3.0  # não normalizado de propósito
        return [SimpleNamespace(values=v.tolist()) for v in vecs]


@override_settings(ORBIT={**ORBIT, "VECTOR_STORE": "pinecone", "PINECONE_API_KEY": "pc-test",
                          "PINECONE_INDEX": "orbit-test", "PINECONE_NAMESPACE_PREFIX": "t-"})
class PineconeStoreTests(Base):
    def setUp(self):
        super().setUp()
        self.pc = FakePinecone()
        vectorstore.set_store("pinecone", vectorstore.PineconeVectorStore(client=self.pc))
        self.addCleanup(vectorstore.set_store, "pinecone", None)

    def ns(self, user=None):
        return self.pc.index_obj.ns.get(f"t-u{(user or self.user).pk}", {})

    def test_ingest_search_reindex_and_forget_go_through_pinecone(self):
        out = store.add_text(self.user, "Fechamento", LONG + "\n\n" + LONG.replace("dia", "data"))
        self.assertEqual(self.pc.created, [("orbit-test", 512, "cosine")])  # índice criado com a dimensão certa
        doc = Document.objects.get(user=self.user)
        self.assertEqual(set(self.ns()), {f"{doc.pk}:{i}" for i in range(out["chunks"])})
        meta = next(iter(self.ns().values()))["metadata"]
        self.assertEqual((meta["source"], meta["type"], meta["user_id"]), ("Fechamento", "text", self.user.pk))

        hits = store.search(self.user, "quando começa o fechamento mensal?", min_score=0.05)
        self.assertTrue(hits and hits[0]["source"] == "Fechamento")

        # reindexar com texto menor apaga os vetores que sobraram
        store.add_text(self.user, "Fechamento", "Texto novo e curto sobre fechamento.")
        self.assertEqual(set(self.ns()), {f"{doc.pk}:0"})
        store.forget_document(self.user, doc.pk)
        self.assertEqual(self.ns(), {})

    def test_users_are_isolated_by_namespace(self):
        store.add_text(self.user, "Segredo", LONG)
        other = User.objects.create_user("b@x.com", "b@x.com", "senha-forte-123")
        self.assertEqual(store.search(other, "fechamento mensal", min_score=0.0), [])
        # mesmo que um vetor de outro usuário apareça no namespace, ele é descartado (doc não é do usuário)
        self.pc.index_obj.ns[f"t-u{other.pk}"] = dict(self.ns())
        self.assertEqual(store.search(other, "fechamento mensal", min_score=0.0), [])

    def test_falls_back_to_local_search_when_pinecone_fails(self):
        store.add_text(self.user, "Fechamento", LONG)
        self.pc.index_obj.fail_query = True
        hits = store.search(self.user, "fechamento mensal", min_score=0.05)
        self.assertTrue(hits and hits[0]["source"] == "Fechamento")

    def test_failed_upsert_rolls_back(self):
        with mock.patch.object(self.pc.index_obj, "upsert", side_effect=ConnectionError("rede")):
            with self.assertRaises(UserFacingError):
                store.add_text(self.user, "X", LONG)
        self.assertFalse(Document.objects.filter(user=self.user).exists())

    def test_dimension_mismatch_is_explained(self):
        vectorstore.set_store("pinecone", vectorstore.PineconeVectorStore(client=FakePinecone(dimension=1024)))
        with self.assertRaisesMessage(UserFacingError, "dimensão 1024"):
            store.add_text(self.user, "X", LONG)

    def test_dashboard_reports_vector_store(self):
        k = self.client.get("/api/dashboard/").json()["knowledge"]
        self.assertEqual((k["vector_store"], k["index"]), ("pinecone", "orbit-test"))


@override_settings(ORBIT={**ORBIT, "EMBEDDING_BACKEND": "pinecone", "PINECONE_API_KEY": "pc-test"})
class PineconeEmbedderTests(Base):
    def test_semantic_embeddings_via_pinecone_inference(self):
        pc = FakePinecone()
        emb = PineconeEmbedder("multilingual-e5-large", client=pc)
        with mock.patch.object(embeddings, "PineconeEmbedder", return_value=emb):
            embeddings._instance = None
            self.addCleanup(setattr, embeddings, "_instance", None)
            store.add_text(self.user, "Fechamento", LONG)
            store.search(self.user, "fechamento", min_score=0.0)
            store.recall(self.user, "fechamento")  # mesma pergunta: vetor da consulta vem do cache
        self.assertEqual(emb.dim, 1024)
        kinds = [c[2] for c in pc.embed_calls]
        self.assertEqual(kinds, ["passage", "query"])
        vec = emb.embed(["fechamento mensal do financeiro"])[0]
        self.assertAlmostEqual(float((vec ** 2).sum()), 1.0, places=4)  # normalizado


# ------------------------------------------------------------------ rastreio: o agente usou a base vetorial?
class RagTracingTests(Base):
    def test_agent_run_records_what_was_retrieved(self):
        store.add_text(self.user, "Fechamento mensal", LONG)
        run_id = self.post("/api/runs/", {"prompt": "quando começa o fechamento mensal?"}).json()["run_id"]
        events = self.client.get(f"/api/runs/{run_id}/events/?after=0").json()["events"]
        ctx = next(e["payload"] for e in events if e["kind"] == "context")
        self.assertEqual((ctx["store"], ctx["fallback"], ctx["chunks"]), ("local", False, 1))
        self.assertEqual(ctx["items"][0]["source"], "Fechamento mensal")
        self.assertGreater(ctx["items"][0]["score"], 0.22)
        r = Retrieval.objects.get(user=self.user, origin="auto")
        self.assertEqual((r.run_id, r.store, r.hits), (run_id, "local", 1))
        dash = self.client.get("/api/dashboard/").json()
        self.assertEqual(dash["rag"]["runs_with_context"], 1)
        self.assertEqual(dash["recent"][0]["rag"]["hits"], 1)

    def test_empty_base_emits_no_context_and_logs_nothing(self):
        self.post("/api/runs/", {"prompt": "olá"})
        self.assertFalse(Retrieval.objects.exists())

    def test_rag_search_tool_reports_store_and_is_logged(self):
        from apps.agent.tools import ToolContext, execute
        store.add_text(self.user, "Fechamento mensal", LONG)
        res = execute("rag_search", {"query": "fechamento mensal"}, ToolContext(user=self.user))
        self.assertEqual(res.data["store"], "local")
        self.assertEqual(Retrieval.objects.get(origin="tool").hits, len(res.data["results"]))

    def test_dashboard_test_search_and_health(self):
        store.add_text(self.user, "Fechamento mensal", LONG)
        res = self.post("/api/dashboard/rag-test/", {"query": "quando começa o fechamento mensal?"}).json()
        self.assertEqual(res["store"], "local")
        self.assertTrue(res["results"][0]["used"])
        self.assertEqual(self.post("/api/dashboard/rag-test/", {"query": ""}).status_code, 400)
        h = self.client.get("/api/dashboard/rag-health/").json()
        self.assertEqual((h["store"], h["ok"], h["in_sync"]), ("local", True, True))
        self.assertEqual(self.client.get("/dashboard/").status_code, 200)


@override_settings(ORBIT={**ORBIT, "VECTOR_STORE": "pinecone", "PINECONE_API_KEY": "pc-test",
                          "PINECONE_INDEX": "orbit-test", "PINECONE_NAMESPACE_PREFIX": ""})
class PineconeTracingTests(Base):
    def setUp(self):
        super().setUp()
        self.pc = FakePinecone()
        vectorstore.set_store("pinecone", vectorstore.PineconeVectorStore(client=self.pc))
        self.addCleanup(vectorstore.set_store, "pinecone", None)

    def test_health_compares_pinecone_with_local_db(self):
        out = store.add_text(self.user, "Fechamento", LONG)
        h = self.client.get("/api/dashboard/rag-health/").json()
        self.assertEqual((h["store"], h["ok"], h["remote_vectors"], h["local_chunks"], h["in_sync"]),
                         ("pinecone", True, out["chunks"], out["chunks"], True))
        self.assertEqual(h["namespace"], f"u{self.user.pk}")
        self.pc.index_obj.ns[f"u{self.user.pk}"].clear()  # vetores sumiram do Pinecone: fora de sincronia
        self.assertFalse(self.client.get("/api/dashboard/rag-health/").json()["in_sync"])
        self.pc.index_obj.fail_query = True
        h = self.client.get("/api/dashboard/rag-health/").json()
        self.assertFalse(h["ok"])
        self.assertIn("ConnectionError", h["error"])

    def test_searches_say_pinecone_or_fallback(self):
        store.add_text(self.user, "Fechamento", LONG)
        info = store.search_traced(self.user, "fechamento mensal", min_score=0.05, origin="test")
        self.assertEqual((info["store"], info["fallback"]), ("pinecone", False))
        self.pc.index_obj.fail_query = True
        info = store.search_traced(self.user, "fechamento mensal", min_score=0.05, origin="test")
        self.assertEqual((info["store"], info["fallback"]), ("local", True))
        self.assertTrue(info["hits"])
        self.assertEqual(list(Retrieval.objects.order_by("created_at", "id").values_list("store", "fallback")),
                         [("pinecone", False), ("local", True)])
        rag = self.client.get("/api/dashboard/").json()["rag"]
        self.assertEqual((rag["by_store"], rag["fallbacks"]), ({"pinecone": 1, "local": 1}, 1))
