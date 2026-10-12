"""Harness checks that do not call a provider, Redis, or Postgres."""
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("GROQ_API_KEY", None)
os.environ.pop("NVIDIA_API_KEY", None)
os.environ.pop("OLLAMA_BASE_URL", None)
os.environ.pop("LLM_PROVIDER", None)
os.environ.pop("LANGCACHE_SERVER_URL", None)
os.environ.pop("LANGCACHE_API_KEY", None)
os.environ.pop("LANGCACHE_CACHE_ID", None)
os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
os.environ.pop("LANGFUSE_SECRET_KEY", None)
os.environ.pop("NL2SQL_MEMORY_DSN", None)

from openai import OpenAIError  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from nl2sql import config  # noqa: E402
from nl2sql.agent.evidence import catalog_kind  # noqa: E402
from nl2sql.agent.graph import Draft, Route, build_graph  # noqa: E402
from nl2sql.agent.guardrails import GuardrailError, check  # noqa: E402
from nl2sql.llm import cost  # noqa: E402
from nl2sql.llm import registry as llm_registry  # noqa: E402
from nl2sql.services import limits, memory  # noqa: E402
from nl2sql.services.limits import BudgetExceeded, RateLimitExceeded  # noqa: E402
import nl2sql.agent.graph as graph  # noqa: E402
import nl2sql.agent.sandbox as sandbox  # noqa: E402
import nl2sql.llm.service as llm_service  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.store: dict[str, float] = {}

    def ping(self):
        return True

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return int(self.store[key])

    def incrbyfloat(self, key, amount):
        self.store[key] = float(self.store.get(key, 0)) + float(amount)
        return self.store[key]

    def expire(self, key, ttl):
        return True

    def get(self, key):
        return self.store.get(key)


class FakeMessage:
    def __init__(self, content):
        self.content = content


def _schema_rows():
    return {
        "columns": ["table_name", "column_name", "data_type"],
        "rows": [["customers", "id", "int"], ["customers", "email", "text"], ["orders", "id", "int"]],
    }


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        llm_registry.reset()

    def test_selects_only_configured_providers_in_order(self):
        os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434"
        os.environ["GROQ_API_KEY"] = "g-test"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("NVIDIA_API_KEY", None)
        os.environ.pop("LLM_PROVIDER", None)
        llm_registry.reset()
        found = llm_registry.entries()
        self.assertEqual([item.provider for item in found], ["groq", "ollama"])
        self.assertEqual(found[llm_registry.start_index(found)].provider, "groq")
        self.assertTrue(llm_registry.ollama_openai_base().endswith("/v1"))
        self.assertFalse(llm_registry.ollama_host().endswith("/v1"))

    def test_provider_override_must_be_enabled(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["LLM_PROVIDER"] = "nvidia"
        os.environ.pop("NVIDIA_API_KEY", None)
        llm_registry.reset()
        with self.assertRaises(RuntimeError):
            llm_registry.ensure_configured()

    def test_none_configured(self):
        for key in ("OPENAI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "OLLAMA_BASE_URL", "LLM_PROVIDER"):
            os.environ.pop(key, None)
        llm_registry.reset()
        with self.assertRaises(RuntimeError):
            llm_registry.ensure_configured()


class CostAndLimitTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        limits.set_client(self.redis)
        os.environ["LLM_DAILY_BUDGET_USD"] = "1"
        os.environ["RATE_LIMIT_PER_MINUTE"] = "2"

    def tearDown(self):
        limits.set_client(None)

    def test_unknown_model_is_priced_high_and_ollama_is_free(self):
        self.assertGreater(cost.estimate("openai", "missing-model", 1000, 0), 0)
        self.assertEqual(cost.estimate("ollama", "llama3.2", 5000, 5000), 0.0)
        self.assertEqual(cost.count_text("", "gpt-5.4-mini"), 0)

    def test_budget_refunds_when_the_reservation_does_not_fit(self):
        limits.reserve("ada", 0.4)
        with self.assertRaises(BudgetExceeded):
            limits.reserve("ada", 0.7)
        self.assertAlmostEqual(self.redis.store[limits.budget_key("ada")], 0.4)
        limits.reconcile("ada", 0.4, 0.1)
        self.assertAlmostEqual(self.redis.store[limits.budget_key("ada")], 0.1)

    def test_rate_limit_counts_a_minute_bucket(self):
        limits.check_rate("ada")
        limits.check_rate("ada")
        with self.assertRaises(RateLimitExceeded):
            limits.check_rate("ada")
        limits.check_rate("other")


class GuardTests(unittest.TestCase):
    def test_denied_column_and_single_statement(self):
        policy = config.Policy(
            "analyst",
            frozenset({"orders", "customers", "products"}),
            denied_columns=frozenset({"customers.email", "customers.phone"}),
        )
        with self.assertRaises(GuardrailError):
            check("SELECT email FROM customers", policy)
        with self.assertRaises(GuardrailError):
            check("SELECT id FROM customers; SELECT id FROM orders", policy)
        checked = check("SELECT id FROM customers", policy)
        self.assertEqual(checked.kind, "read")
        self.assertIn("LIMIT", checked.sql)


class GraphTests(unittest.TestCase):
    def setUp(self):
        graph._schema_cache.clear()
        self.sandbox_calls = []
        self.sandbox_dsns = []
        self.original_run = sandbox.run
        self.original_call = llm_service.llm_service.call_sync
        sandbox.run = self._run
        for key in ("LANGCACHE_SERVER_URL", "LANGCACHE_API_KEY", "LANGCACHE_CACHE_ID"):
            os.environ.pop(key, None)

    def tearDown(self):
        sandbox.run = self.original_run
        llm_service.llm_service.call_sync = self.original_call
        graph._schema_cache.clear()

    def _run(self, mode, sql, max_rows=500, dsn=None):
        self.sandbox_calls.append(mode)
        self.sandbox_dsns.append(dsn)
        kind = catalog_kind(sql)
        if kind == "foreign_keys":
            return {"columns": ["table_name", "column_name", "foreign_table", "foreign_column"], "rows": []}
        if kind == "values":
            return {"columns": ["value"], "rows": []}
        if kind == "columns":
            return _schema_rows()
        if mode == "explain":
            return {"cost": 10.0}
        if mode == "dry_write":
            return {"rowcount": 2}
        if mode == "write":
            return {"rowcount": 2}
        if mode == "read":
            return {"columns": ["id"], "rows": [[7]]}
        raise AssertionError(mode)

    def _install_model(self, intents, drafts, answers=None):
        drafts = list(drafts)
        answers = list(answers or ["Ada"])

        def call(messages, response_format=None, **kwargs):
            if response_format is Route:
                return Route(intent=intents.pop(0), reason="because", clarifying_question="Which month?")
            if response_format is Draft:
                return Draft(sql=drafts.pop(0))
            return FakeMessage(answers.pop(0))

        llm_service.llm_service.call_sync = call

    def _invoke(self, role, question="how many customers"):
        app = build_graph(InMemorySaver())
        return app.invoke(
            {"question": question, "role": role, "user_id": "ada", "memory": ""},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )

    def test_checkpointer_is_required(self):
        with self.assertRaises(RuntimeError):
            build_graph(None)

    def test_read_repairs_a_denied_column_then_answers(self):
        shop = config.Policy(
            "analyst",
            frozenset({"orders", "customers", "products"}),
            denied_columns=frozenset({"customers.email", "customers.phone"}),
        )
        self._install_model(["query"], ["SELECT email FROM customers", "SELECT id FROM customers"])
        with patch.dict(config.ROLES, {"analyst": shop}):
            result = self._invoke("analyst")
        self.assertIn("Ada", result["answer"])
        self.assertTrue(result["remember"])
        self.assertTrue(any("guardrail" in line for line in result["trace"]))
        self.assertIn("read", self.sandbox_calls)
        self.assertNotIn("write", self.sandbox_calls)
        self.assertNotIn("__interrupt__", result)

    def test_write_without_permission_stops_at_route(self):
        self._install_model(["write"], [])
        result = self._invoke("analyst", "delete old orders")
        self.assertIn("not permitted", result["answer"])
        self.assertEqual(self.sandbox_calls, [])
        self.assertNotIn("remember", result)

    def test_write_interrupts_before_execute(self):
        self._install_model(["write"], ["UPDATE orders SET status = 'x' WHERE id = 1"])
        result = self._invoke("ops", "mark order 1 shipped")
        self.assertIn("__interrupt__", result)
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["kind"], "write")
        self.assertIn("dry_write", self.sandbox_calls)
        self.assertNotIn("write", self.sandbox_calls)

    def test_generate_redacts_the_question_and_approval_keeps_it(self):
        seen = []

        def call(messages, response_format=None, **kwargs):
            seen.append(messages)
            return Draft(sql="SELECT id FROM customers")

        llm_service.llm_service.call_sync = call
        state = {
            "question": "email ada@shop.test",
            "role": "analyst",
            "intent": "query",
            "schema": "customers(id integer)",
        }
        graph.generate_node(state, {})
        self.assertEqual(state["question"], "email ada@shop.test")
        self.assertIn("[redacted]", seen[0][1][1])
        self.assertNotIn("ada@shop.test", seen[0][1][1])

        self._install_model(["write"], ["UPDATE orders SET status = 'x' WHERE id = 1"])
        result = self._invoke("ops", "email ada@shop.test")
        payload = result["__interrupt__"][0].value
        self.assertEqual(payload["question"], "email ada@shop.test")

    def test_reject_executes_nothing_and_edit_returns_to_the_guard(self):
        self._install_model(
            ["write"],
            ["UPDATE orders SET status = 'x' WHERE id = 1", "UPDATE orders SET status = 'y' WHERE id = 2"],
        )
        app = build_graph(InMemorySaver())
        cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = app.invoke(
            {"question": "mark order 1", "role": "ops", "user_id": "ada", "memory": ""},
            cfg,
        )
        self.assertIn("__interrupt__", result)
        from langgraph.types import Command

        rejected = app.invoke(Command(resume={"action": "reject"}), cfg)
        self.assertIn("Cancelled", rejected["answer"])
        self.assertNotIn("remember", rejected)
        self.assertNotIn("write", self.sandbox_calls)

        self.sandbox_calls.clear()
        self._install_model(
            ["write"],
            ["UPDATE orders SET status = 'x' WHERE id = 1", "UPDATE orders SET status = 'y' WHERE id = 2"],
        )
        app = build_graph(InMemorySaver())
        cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
        result = app.invoke(
            {"question": "mark order 1", "role": "ops", "user_id": "ada", "memory": ""},
            cfg,
        )
        edited = app.invoke(Command(resume={"action": "edit", "sql": "DROP TABLE orders"}), cfg)
        self.assertTrue(any("guardrail" in line and "human: edited" in "".join(edited["trace"]) or "guardrail" in line
                            for line in edited["trace"]))
        self.assertTrue(any("human: edited" in line for line in edited["trace"]))
        self.assertTrue(any("guardrail" in line for line in edited["trace"]))
        self.assertNotIn("write", self.sandbox_calls)

    def test_cache_hit_skips_sql(self):
        os.environ["LANGCACHE_SERVER_URL"] = "https://cache.example"
        os.environ["LANGCACHE_API_KEY"] = "k"
        os.environ["LANGCACHE_CACHE_ID"] = "c"
        graph.answer_cache.search = lambda *args, **kwargs: "cached customers"
        try:
            self._install_model(["query"], [])
            result = self._invoke("analyst")
        finally:
            for key in ("LANGCACHE_SERVER_URL", "LANGCACHE_API_KEY", "LANGCACHE_CACHE_ID"):
                os.environ.pop(key, None)
        self.assertEqual(result["answer"], "cached customers")
        self.assertEqual(self.sandbox_calls, [])
        self.assertTrue(result["remember"])

    def test_budget_stop_does_not_touch_sql(self):
        def call(*args, **kwargs):
            raise BudgetExceeded("Daily model budget of $1.00 is exhausted for this user.")

        llm_service.llm_service.call_sync = call
        result = self._invoke("analyst")
        self.assertIn("budget", result["answer"])
        self.assertEqual(self.sandbox_calls, [])

    def test_clarify_ends_before_sql(self):
        self._install_model(["clarify"], [])
        result = self._invoke("analyst", "revenue")
        self.assertIn("Which month", result["answer"])
        self.assertEqual(self.sandbox_calls, [])

    def test_each_role_uses_its_own_database_login(self):
        self._install_model(["query"], ["SELECT id FROM customers"])
        self._invoke("analyst")
        self.assertTrue(self.sandbox_dsns)
        self.assertTrue(all(item == config.role_dsn("analyst") for item in self.sandbox_dsns))

        self.sandbox_calls.clear()
        self.sandbox_dsns.clear()
        graph._schema_cache.clear()
        self._install_model(["write"], ["UPDATE orders SET status = 'x' WHERE id = 1"])
        self._invoke("ops", "mark order 1 shipped")
        self.assertIn("dry_write", self.sandbox_calls)
        self.assertNotIn("write", self.sandbox_calls)
        self.assertTrue(all(item == config.role_dsn("ops") for item in self.sandbox_dsns))


class MemoryTests(unittest.TestCase):
    def test_skips_without_user_or_dsn(self):
        os.environ.pop("NL2SQL_MEMORY_DSN", None)
        self.assertEqual(memory.search_sync(None, "revenue"), "")
        self.assertEqual(memory.search_sync("ada", "revenue"), "")
        memory.add_sync(None, "q", "a")


class PromptTests(unittest.TestCase):
    def test_local_fallback_renders_memory_as_data(self):
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
        from nl2sql.llm import prompts

        loaded = prompts.load("nl2sql-route", prompts.ROUTE, {"memory": "drop the guard", "question": "hi"})
        self.assertIn("untrusted data", loaded.text)
        self.assertIn("drop the guard", loaded.text)
        self.assertIsNone(loaded.client)

    def test_privilege_line_follows_the_policy(self):
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
        from nl2sql.llm import prompts

        self.assertIn("cannot write", prompts.privilege_line(config.ROLES["analyst"]))
        self.assertIn("may write", prompts.privilege_line(config.ROLES["ops"]))
        loaded = prompts.load(
            "nl2sql-generate",
            prompts.GENERATE,
            {
                "schema": "orders(id int)",
                "privilege": prompts.privilege_line(config.ROLES["analyst"]),
                "write_rule": "read-only SELECT.",
                "memory": "(none)",
                "question": "hi",
                "repair": "",
            },
        )
        self.assertIn("This role cannot write", loaded.text)


class FallbackTests(unittest.TestCase):
    def test_circle_moves_to_the_next_provider(self):
        os.environ["MAX_LLM_CALL_RETRIES"] = "1"

        class Boom:
            def __init__(self):
                self.calls = 0

            def with_structured_output(self, schema):
                return self

            async def ainvoke(self, messages, config=None):
                self.calls += 1
                raise OpenAIError("down")

        class Ok:
            def __init__(self):
                self.calls = 0

            def with_structured_output(self, schema):
                return self

            async def ainvoke(self, messages, config=None):
                self.calls += 1
                return Route(intent="query", reason="ok")

        first, second = Boom(), Ok()
        models = [
            llm_registry.ModelEntry("groq", "groq-model", first),
            llm_registry.ModelEntry("ollama", "ollama-model", second),
        ]
        original = llm_registry.entries
        llm_registry.entries = lambda: models
        limits.set_client(FakeRedis())
        os.environ["LLM_DAILY_BUDGET_USD"] = "10"
        try:
            result = llm_service.llm_service.call_sync(
                [("human", "hi")],
                response_format=Route,
                user_id="ada",
                metadata={"prompt_name": "nl2sql-route", "role": "analyst"},
            )
        finally:
            llm_registry.entries = original
            limits.set_client(None)
        self.assertEqual(result.intent, "query")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    def test_requested_provider_starts_the_circle(self):
        os.environ["MAX_LLM_CALL_RETRIES"] = "1"

        class Boom:
            def with_structured_output(self, schema):
                return self

            async def ainvoke(self, messages, config=None):
                raise OpenAIError("down")

        class Ok:
            def __init__(self):
                self.calls = 0

            def with_structured_output(self, schema):
                return self

            async def ainvoke(self, messages, config=None):
                self.calls += 1
                return Route(intent="query", reason="ok")

        skipped, chosen = Boom(), Ok()
        models = [
            llm_registry.ModelEntry("groq", "groq-model", skipped),
            llm_registry.ModelEntry("ollama", "ollama-model", chosen),
        ]
        original = llm_registry.entries
        llm_registry.entries = lambda: models
        limits.set_client(FakeRedis())
        os.environ["LLM_DAILY_BUDGET_USD"] = "10"
        try:
            result = llm_service.llm_service.call_sync(
                [("human", "hi")],
                response_format=Route,
                user_id="ada",
                metadata={"prompt_name": "nl2sql-route", "role": "analyst"},
                provider="ollama",
            )
        finally:
            llm_registry.entries = original
            limits.set_client(None)
        self.assertEqual(result.intent, "query")
        self.assertEqual(chosen.calls, 1)

    def test_provider_index_names_the_enabled_entry(self):
        models = [
            llm_registry.ModelEntry("groq", "g", None),
            llm_registry.ModelEntry("ollama", "o", None),
        ]
        self.assertEqual(llm_registry.provider_index(models, "ollama"), 1)
        with self.assertRaises(RuntimeError):
            llm_registry.provider_index(models, "openai")
        with patch.object(llm_registry, "entries", return_value=[]):
            self.assertEqual(llm_registry.catalog(), [])


class _Snap:
    def __init__(self, values, interrupts):
        self.values = values
        self.interrupts = interrupts


class _Interrupt:
    def __init__(self, value):
        self.value = value


class ScriptedGraph:
    def __init__(self):
        self.values = {}
        self.interrupts = []
        self.results = []
        self.payloads = []

    def get_state(self, cfg):
        return _Snap(self.values, self.interrupts)

    def invoke(self, payload, cfg):
        self.payloads.append(payload)
        result = self.results.pop(0)
        if result.get("__interrupt__"):
            self.interrupts = result["__interrupt__"]
            self.values = {
                "user_id": "ada",
                "role": "analyst",
                "question": "how many customers",
                "trace": ["route:query"],
            }
        else:
            self.interrupts = []
            self.values = {"user_id": "ada", "role": "analyst", "trace": result.get("trace") or []}
        return result


class MemorySessions:
    def __init__(self):
        self.data = {}
        self.issued = 0

    def create_session(self, user_id):
        self.issued += 1
        token = f"token-{self.issued}"
        self.data[token] = user_id
        return token, 60

    def read_session(self, token):
        return self.data.get(token)

    def delete_session(self, token):
        self.data.pop(token, None)


class MemoryStore:
    def __init__(self):
        self.data = {}

    def set(self, key, value, ex=None):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


class AuthTests(unittest.TestCase):
    def test_lookup_accepts_a_known_role_only(self):
        from nl2sql.services.auth import AuthError, authenticate, get_user

        def lookup(user_id, password):
            if user_id == "ada" and password == "ada":
                return "analyst"
            return None

        principal = authenticate("ada", "ada", lookup=lookup)
        self.assertEqual(principal.user_id, "ada")
        self.assertEqual(principal.role, "analyst")
        with self.assertRaises(AuthError):
            authenticate("ada", "nope", lookup=lookup)
        with self.assertRaises(AuthError):
            authenticate("ada", "ada", lookup=lambda user_id, password: "superuser")
        with self.assertRaises(AuthError):
            get_user("missing", lookup=lambda user_id: None)
        with self.assertRaises(AuthError):
            get_user("ada", lookup=lambda user_id: "root")

    def test_unknown_role_has_no_database_login(self):
        with self.assertRaises(KeyError):
            config.role_dsn("root")

    def test_session_stores_the_user_id_only(self):
        from nl2sql.services import sessions

        store = MemoryStore()
        token, ttl = sessions.create_session("ada", store=store)
        self.assertGreater(ttl, 0)
        self.assertEqual(store.data[f"session:{token}"], "ada")
        self.assertEqual(sessions.read_session(token, store=store), "ada")
        sessions.delete_session(token, store=store)
        self.assertIsNone(sessions.read_session(token, store=store))


class ApiTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("NL2SQL_MEMORY_DSN", None)
        os.environ["RATE_LIMIT_PER_MINUTE"] = "30"
        self.redis = FakeRedis()
        limits.set_client(self.redis)
        self.sessions = MemorySessions()
        self.graph = ScriptedGraph()

    def tearDown(self):
        limits.set_client(None)

    def _users(self):
        from nl2sql.services.auth import AuthError, Principal

        users = {"ada": Principal("ada", "analyst"), "grace": Principal("grace", "ops")}

        def authenticator(user_id, password):
            if password == user_id and user_id in users:
                return users[user_id]
            raise AuthError()

        def user_loader(user_id):
            if user_id not in users:
                raise AuthError()
            return users[user_id]

        return authenticator, user_loader

    def _client(self, **kwargs):
        from fastapi.testclient import TestClient

        from nl2sql.api.app import create_app

        authenticator, user_loader = self._users()
        app = create_app(
            graph=kwargs.get("graph", self.graph),
            authenticator=authenticator,
            user_loader=user_loader,
            session_store=self.sessions,
            readiness=kwargs.get("readiness", lambda: True),
        )
        return TestClient(app)

    def _login(self, client, user_id="ada"):
        response = client.post("/v1/auth/login", json={"user_id": user_id, "password": user_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def test_failed_login_is_unauthorized(self):
        with self._client() as client:
            response = client.post("/v1/auth/login", json={"user_id": "ada", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], {"code": "authentication_failed", "message": "authentication failed"})

    def test_bearer_question_returns_the_answer(self):
        self.graph.results = [{"answer": "Ada", "trace": ["route:query"], "remember": False}]
        with self._client() as client:
            token = self._login(client)
            response = client.post(
                "/v1/questions",
                headers={"Authorization": f"Bearer {token}"},
                json={"question": "how many customers"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "answered")
        self.assertEqual(body["answer"], "Ada")
        self.assertEqual(self.graph.payloads[0]["role"], "analyst")
        self.assertEqual(self.graph.payloads[0]["user_id"], "ada")

    def test_write_returns_an_approval_payload(self):
        self.graph.results = [{
            "__interrupt__": [_Interrupt({"type": "sql_approval", "kind": "write", "sql": "UPDATE orders SET status = 'x' WHERE id = 1"})],
        }]
        with self._client() as client:
            token = self._login(client)
            response = client.post(
                "/v1/questions",
                headers={"Authorization": f"Bearer {token}"},
                json={"question": "mark order 1 shipped"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "awaiting_approval")
        self.assertEqual(body["approval"]["kind"], "write")
        self.assertIn("thread_id", body)

    def test_edit_without_sql_is_rejected(self):
        with self._client() as client:
            token = self._login(client)
            response = client.post(
                "/v1/questions/thread-1/decision",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "edit"},
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_body")

    def test_decision_from_another_user_is_forbidden(self):
        self.graph.values = {"user_id": "grace", "role": "ops", "question": "mark order 1"}
        self.graph.interrupts = [_Interrupt({"type": "sql_approval", "kind": "write"})]
        with self._client() as client:
            token = self._login(client, "ada")
            response = client.post(
                "/v1/questions/thread-1/decision",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "approve"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "thread_forbidden")
        self.assertEqual(self.graph.payloads, [])

    def test_logout_makes_the_token_unusable(self):
        self.graph.results = [{"answer": "Ada", "trace": ["route:query"], "remember": False}]
        with self._client() as client:
            token = self._login(client)
            gone = client.post("/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(gone.status_code, 204)
            response = client.post(
                "/v1/questions",
                headers={"Authorization": f"Bearer {token}"},
                json={"question": "how many customers"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_failed")

    def test_ready_reports_dependency_failure(self):
        with self._client(readiness=lambda: False) as client:
            self.assertEqual(client.get("/health/live").status_code, 200)
            ready = client.get("/health/ready")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()["error"]["code"], "not_ready")

    def test_rate_limit_is_http_429(self):
        os.environ["RATE_LIMIT_PER_MINUTE"] = "1"
        self.graph.results = [{"answer": "Ada", "trace": [], "remember": False}]
        with self._client() as client:
            token = self._login(client)
            headers = {"Authorization": f"Bearer {token}"}
            first = client.post("/v1/questions", headers=headers, json={"question": "how many customers"})
            second = client.post("/v1/questions", headers=headers, json={"question": "how many customers"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "rate_limited")

    def test_me_returns_the_auth_row(self):
        with self._client() as client:
            missing = client.get("/v1/me")
            token = self._login(client)
            response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"user_id": "ada", "role": "analyst"})

    def test_executed_turn_includes_the_rows(self):
        self.graph.results = [{
            "answer": "1",
            "trace": ["executed read: 1 rows", "respond"],
            "sql": "SELECT count(*) AS count FROM customers",
            "columns": ["count"],
            "rows": [["1"]],
        }]
        with self._client() as client:
            token = self._login(client)
            response = client.post(
                "/v1/questions",
                headers={"Authorization": f"Bearer {token}"},
                json={"question": "how many customers"},
            )
        body = response.json()
        self.assertEqual(body["sql"], "SELECT count(*) AS count FROM customers")
        self.assertEqual(body["columns"], ["count"])
        self.assertEqual(body["rows"], [["1"]])
        self.assertEqual(body["rowcount"], 1)

    def test_write_turn_includes_the_rowcount(self):
        self.graph.results = [{
            "answer": "Done. 1 row(s) affected.",
            "trace": ["human: approved", "executed write"],
            "sql": "UPDATE orders SET status = 'paid' WHERE id = 1",
            "rowcount": 1,
        }]
        with self._client() as client:
            token = self._login(client)
            response = client.post(
                "/v1/questions",
                headers={"Authorization": f"Bearer {token}"},
                json={"question": "mark order 1 paid"},
            )
        body = response.json()
        self.assertEqual(body["rowcount"], 1)
        self.assertNotIn("columns", body)

    def test_rejection_omits_sql(self):
        self.graph.results = [{
            "answer": "Cancelled by reviewer. Nothing was executed.",
            "trace": ["human: rejected"],
            "sql": "UPDATE orders SET status = 'x' WHERE id = 1",
        }]
        with self._client() as client:
            token = self._login(client)
            response = client.post(
                "/v1/questions",
                headers={"Authorization": f"Bearer {token}"},
                json={"question": "mark order 1 shipped"},
            )
        body = response.json()
        self.assertEqual(body["answer"], "Cancelled by reviewer. Nothing was executed.")
        self.assertNotIn("sql", body)

    def test_runs_are_empty_without_an_audit_dsn(self):
        previous = os.environ.pop("NL2SQL_AUDIT_DSN", None)
        try:
            with self._client() as client:
                token = self._login(client)
                response = client.get("/v1/runs", headers={"Authorization": f"Bearer {token}"})
        finally:
            if previous is not None:
                os.environ["NL2SQL_AUDIT_DSN"] = previous
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"runs": []})

    def test_runs_are_limited_to_the_signed_in_user(self):
        from nl2sql.services import audit

        seen = {}

        def fake(user_id, connect=None):
            seen["user_id"] = user_id
            return [{
                "created_at": "2026-10-12T00:00:00+00:00",
                "sql": "SELECT 1",
                "rowcount": 1,
                "decision": "executed",
            }]

        with patch.object(audit, "list_for", fake):
            with self._client() as client:
                token = self._login(client)
                response = client.get("/v1/runs", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(seen["user_id"], "ada")
        self.assertEqual(response.json()["runs"][0]["sql"], "SELECT 1")
        self.assertEqual(response.json()["runs"][0]["decision"], "executed")

    def test_spend_reads_todays_reservation(self):
        with self._client() as client:
            token = self._login(client)
            headers = {"Authorization": f"Bearer {token}"}
            empty = client.get("/v1/spend", headers=headers)
            limits.reserve("ada", 1.25)
            response = client.get("/v1/spend", headers=headers)
        self.assertEqual(empty.json()["spent"], 0)
        self.assertEqual(response.json()["spent"], 1.25)
        self.assertEqual(response.json()["cap"], config.llm_daily_budget_usd())

    def test_models_list_configured_chat_models(self):
        from nl2sql.llm import registry as llm_registry

        models = [
            llm_registry.ModelEntry("groq", "llama-3.3-70b-versatile", None),
            llm_registry.ModelEntry("ollama", "llama3.2", None),
        ]
        with patch.object(llm_registry, "entries", return_value=models), patch.object(config, "llm_provider", return_value="ollama"):
            with self._client() as client:
                token = self._login(client)
                response = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        body = response.json()["models"]
        self.assertEqual(body[0]["provider"], "groq")
        self.assertFalse(body[0]["start"])
        self.assertEqual(body[1], {"provider": "ollama", "model": "llama3.2", "start": True})
        self.assertNotIn("llm", body[0])

    def test_unknown_provider_is_rejected(self):
        from nl2sql.llm import registry as llm_registry

        models = [llm_registry.ModelEntry("groq", "llama-3.3-70b-versatile", None)]
        self.graph.results = [{"answer": "Ada", "trace": [], "remember": False}]
        with patch.object(llm_registry, "entries", return_value=models):
            with self._client() as client:
                token = self._login(client)
                response = client.post(
                    "/v1/questions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"question": "how many customers", "provider": "openai"},
                )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unknown_provider")
        self.assertEqual(self.graph.payloads, [])

    def test_chosen_provider_starts_that_question(self):
        from nl2sql.llm import registry as llm_registry

        models = [llm_registry.ModelEntry("groq", "llama-3.3-70b-versatile", None)]
        self.graph.results = [{"answer": "Ada", "trace": ["route:query"], "remember": False}]
        with patch.object(llm_registry, "entries", return_value=models):
            with self._client() as client:
                token = self._login(client)
                response = client.post(
                    "/v1/questions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"question": "how many customers", "provider": "groq"},
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.graph.payloads[0]["provider"], "groq")

    def test_blank_provider_is_omitted(self):
        self.graph.results = [{"answer": "Ada", "trace": ["route:query"], "remember": False}]
        with self._client() as client:
            token = self._login(client)
            response = client.post(
                "/v1/questions",
                headers={"Authorization": f"Bearer {token}"},
                json={"question": "how many customers", "provider": "  "},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("provider", self.graph.payloads[0])


if __name__ == "__main__":
    unittest.main()
