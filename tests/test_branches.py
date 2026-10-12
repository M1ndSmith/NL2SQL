"""Branch coverage for the harness. No real database, Redis, or model provider."""
import os
import queue
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
os.environ.pop("NL2SQL_MEMORY_DSN", None)
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from nl2sql import config  # noqa: E402
from nl2sql.agent.graph import Draft, Route, build_graph  # noqa: E402
from nl2sql.agent.guardrails import GuardrailError, check  # noqa: E402
from nl2sql.agent.sandbox import SandboxError  # noqa: E402
from nl2sql.api.app import create_app  # noqa: E402
from nl2sql.api.routes import health  # noqa: E402
from nl2sql.application import questions  # noqa: E402
from nl2sql.application.questions import ThreadForbidden  # noqa: E402
from nl2sql.services import cache, checkpoint, limits  # noqa: E402
from nl2sql.services.auth import AuthError, Principal  # noqa: E402
from nl2sql.services.limits import RateLimitExceeded  # noqa: E402
import nl2sql.agent.graph as graph  # noqa: E402
from nl2sql.agent.evidence import catalog_kind  # noqa: E402
import nl2sql.agent.sandbox as sandbox  # noqa: E402
import nl2sql.llm.service as llm_service  # noqa: E402
import nl2sql.services.auth as auth  # noqa: E402
import run  # noqa: E402


# The shop catalog is sample data. Default roles discover tables from the login.
ANALYST = config.Policy(
    "analyst",
    frozenset({"orders", "customers", "products"}),
    denied_columns=frozenset({"customers.email", "customers.phone"}),
)
OPS = config.Policy(
    "ops",
    frozenset({"orders", "customers", "products", "inventory"}),
    can_write=True,
)
_LANGCACHE = ("LANGCACHE_SERVER_URL", "LANGCACHE_API_KEY", "LANGCACHE_CACHE_ID")


def _schema_rows():
    return {
        "columns": ["table_name", "column_name", "data_type"],
        "rows": [["customers", "id", "int"], ["customers", "email", "text"], ["orders", "id", "int"]],
    }


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [type("Col", (), {"name": "id"})()]
        self.rowcount = 4

    def execute(self, sql):
        self.conn.executed.append(sql)

    def fetchone(self):
        return ([{"Plan": {"Total Cost": 9.5}}],)

    def fetchmany(self, limit):
        self.conn.fetched = limit
        return [(1, None, object())]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Conn:
    def __init__(self):
        self.read_only = False
        self.executed = []
        self.rolled_back = False
        self.committed = False
        self.fetched = None

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _SqlError(Exception):
    def __init__(self, sqlstate):
        super().__init__("db failed")
        self.sqlstate = sqlstate


class GuardBranchTests(unittest.TestCase):
    def test_rejected_statements(self):
        cases = [
            ("CREATE TABLE notes (id int)", ANALYST, "not allowed"),
            ("WITH d AS (DELETE FROM orders WHERE id = 1 RETURNING id) SELECT id FROM customers", ANALYST, "nested"),
            ("UPDATE orders SET status = 'x'", OPS, "WHERE"),
            ("SELECT pg_sleep(1)", ANALYST, "pg_sleep"),
            ("SELECT id FROM secret.orders", ANALYST, "not accessible"),
            ("SELECT id FROM inventory", ANALYST, "not permitted"),
            ("SELECT * FROM customers", ANALYST, "SELECT *"),
            ("UPDATE orders SET status = 'x' WHERE id = 1", ANALYST, "not permitted to write"),
        ]
        for sql, policy, fragment in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(GuardrailError) as caught:
                    check(sql, policy)
                self.assertIn(fragment, str(caught.exception))

    def test_ops_delete_with_where_passes(self):
        checked = check("DELETE FROM orders WHERE id = 1", OPS)
        self.assertEqual(checked.kind, "write")
        self.assertEqual(checked.tables, {"orders"})
        self.assertNotIn("LIMIT", checked.sql)

    def test_count_star_and_cte_names_are_allowed(self):
        counted = check("SELECT count(*) FROM customers", ANALYST)
        self.assertEqual(counted.kind, "read")
        self.assertIn("LIMIT", counted.sql)
        cte = check("WITH inventory AS (SELECT id FROM products) SELECT id FROM inventory", ANALYST)
        self.assertEqual(cte.tables, {"products"})

    def test_unparsed_sql_is_rejected(self):
        with self.assertRaises(GuardrailError) as caught:
            check("SELECT (", ANALYST)
        self.assertIn("does not parse", str(caught.exception))
        with self.assertRaises(GuardrailError):
            check("", ANALYST)


class GraphBranchTests(unittest.TestCase):
    def setUp(self):
        graph._schema_cache.clear()
        self.modes = []
        self.stored = []
        self.original_run = sandbox.run
        self.original_call = llm_service.llm_service.call_sync
        self.original_search = graph.answer_cache.search
        self.original_store = graph.answer_cache.store
        graph.answer_cache.search = lambda *args, **kwargs: None
        graph.answer_cache.store = lambda *args, **kwargs: self.stored.append(args)
        for key in _LANGCACHE:
            os.environ.pop(key, None)
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"

    def tearDown(self):
        sandbox.run = self.original_run
        llm_service.llm_service.call_sync = self.original_call
        graph.answer_cache.search = self.original_search
        graph.answer_cache.store = self.original_store
        graph._schema_cache.clear()
        for key in _LANGCACHE:
            os.environ.pop(key, None)

    def _install(self, intents, drafts, answers=None, explain_cost=10.0, read_rows=None, fatal=False, retryable=False):
        intents = list(intents)
        drafts = list(drafts)
        answers = list(answers or ["Ada"])
        reads = {"n": 0}

        def run(mode, sql, max_rows=500, dsn=None):
            self.modes.append(mode)
            kind = catalog_kind(sql)
            if kind == "foreign_keys":
                return {"columns": ["table_name", "column_name", "foreign_table", "foreign_column"], "rows": []}
            if kind == "values":
                return {"columns": ["value"], "rows": []}
            if kind == "columns":
                return _schema_rows()
            if mode == "explain":
                if fatal:
                    raise SandboxError("DB error [42501]: permission denied", False)
                return {"cost": explain_cost}
            if mode == "read":
                reads["n"] += 1
                if retryable and reads["n"] == 1:
                    raise SandboxError("DB error [42601]: syntax", True)
                return {"columns": ["id"], "rows": [] if read_rows is None else read_rows}
            raise AssertionError(mode)

        def call(messages, response_format=None, **kwargs):
            if response_format is Route:
                return Route(intent=intents.pop(0), reason="because")
            if response_format is Draft:
                return Draft(sql=drafts.pop(0))
            return type("Msg", (), {"content": answers.pop(0)})()

        sandbox.run = run
        llm_service.llm_service.call_sync = call

    def _invoke(self, role="analyst"):
        app = build_graph(InMemorySaver())
        return app.invoke(
            {"question": "how many customers", "role": role, "user_id": "ada", "memory": ""},
            {"configurable": {"thread_id": str(uuid.uuid4())}},
        )

    def test_expensive_read_interrupts_before_execute(self):
        self._install(["query"], ["SELECT id FROM customers"], explain_cost=config.COST_APPROVAL_THRESHOLD + 1)
        result = self._invoke()
        self.assertIn("__interrupt__", result)
        self.assertEqual(result["__interrupt__"][0].value["kind"], "read")
        explain_at = self.modes.index("explain")
        self.assertNotIn("read", self.modes[explain_at + 1:])

    def test_fatal_sandbox_error_gives_up(self):
        self._install(["query"], ["SELECT id FROM customers"], fatal=True)
        result = self._invoke()
        self.assertIn("couldn't produce", result["answer"])
        self.assertNotIn("read", [mode for mode in self.modes if mode != "read"] )

    def test_empty_read_answers_without_inventing_rows(self):
        self._install(["query"], ["SELECT id FROM customers"], read_rows=[])
        result = self._invoke()
        self.assertIn("no rows", result["answer"])
        self.assertTrue(result["remember"])

    def test_cheap_read_is_cached_only_when_langcache_is_enabled(self):
        for key in _LANGCACHE:
            os.environ[key] = "set"
        self._install(["query"], ["SELECT id FROM customers"], read_rows=[[7]])
        result = self._invoke()
        self.assertIn("cached answer", result["trace"])
        self.assertTrue(self.stored)

        self.stored.clear()
        self.modes.clear()
        graph._schema_cache.clear()
        for key in _LANGCACHE:
            os.environ.pop(key, None)
        self._install(["query"], ["SELECT id FROM customers"], read_rows=[[7]])
        result = self._invoke()
        self.assertNotIn("cached answer", result["trace"])
        self.assertEqual(self.stored, [])

    def test_retryable_execute_failure_repairs_then_answers(self):
        self._install(
            ["query"],
            ["SELECT missing FROM customers", "SELECT id FROM customers"],
            read_rows=[[7]],
            retryable=True,
        )
        result = self._invoke()
        self.assertIn("Ada", result["answer"])
        self.assertTrue(any("execute failed" in line for line in result["trace"]))


class SandboxBranchTests(unittest.TestCase):
    def tearDown(self):
        sandbox.config.WALL_CLOCK_S = config.WALL_CLOCK_S

    def _drive(self, mode, connect, sql="SELECT 1"):
        import psycopg
        import resource

        with patch.object(resource, "setrlimit", lambda *args, **kwargs: None), \
                patch.object(psycopg, "connect", connect), \
                patch.object(sandbox.mp, "get_context", lambda method: _inline_context(start=True)):
            return sandbox.run(mode, sql, dsn="postgresql://example", max_rows=5)

    def test_missing_dsn_fails_closed(self):
        with self.assertRaises(SandboxError) as caught:
            sandbox.run("read", "SELECT 1", dsn="")
        self.assertFalse(caught.exception.retryable)

    def test_worker_read_explain_and_writes(self):
        conn = _Conn()
        explained = self._drive("explain", lambda *args, **kwargs: conn)
        self.assertEqual(explained["cost"], 9.5)
        self.assertTrue(conn.read_only)
        self.assertTrue(any(sql.startswith("EXPLAIN") for sql in conn.executed))

        conn = _Conn()
        read = self._drive("read", lambda *args, **kwargs: conn, sql="SELECT id FROM customers")
        self.assertEqual(read["columns"], ["id"])
        self.assertEqual(len(read["rows"][0]), 3)
        self.assertIsNone(read["rows"][0][1])
        self.assertEqual(conn.fetched, 5)

        conn = _Conn()
        dry = self._drive("dry_write", lambda *args, **kwargs: conn, sql="UPDATE orders SET status = 'x' WHERE id = 1")
        self.assertEqual(dry["rowcount"], 4)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

        conn = _Conn()
        wrote = self._drive("write", lambda *args, **kwargs: conn, sql="UPDATE orders SET status = 'x' WHERE id = 1")
        self.assertEqual(wrote["rowcount"], 4)
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)

    def test_unknown_mode_and_sqlstates(self):
        conn = _Conn()
        with self.assertRaises(SandboxError) as unknown:
            self._drive("other", lambda *args, **kwargs: conn)
        self.assertFalse(unknown.exception.retryable)
        self.assertIn("unknown mode", str(unknown.exception))

        def fail(sqlstate):
            def connect(*args, **kwargs):
                raise _SqlError(sqlstate)
            return connect

        with self.assertRaises(SandboxError) as retryable:
            self._drive("read", fail("42601"))
        self.assertTrue(retryable.exception.retryable)

        with self.assertRaises(SandboxError) as denied:
            self._drive("read", fail("42501"))
        self.assertFalse(denied.exception.retryable)

    def test_worker_timeout_is_retryable(self):
        sandbox.config.WALL_CLOCK_S = 0.01
        with patch.object(sandbox.mp, "get_context", lambda method: _inline_context(start=None)):
            with self.assertRaises(SandboxError) as caught:
                sandbox.run("read", "SELECT 1", dsn="postgresql://example")
        self.assertTrue(caught.exception.retryable)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.saved_password = os.environ.get("NL2SQL_PASSWORD")
        os.environ["NL2SQL_PASSWORD"] = "pw"

    def tearDown(self):
        if self.saved_password is None:
            os.environ.pop("NL2SQL_PASSWORD", None)
        else:
            os.environ["NL2SQL_PASSWORD"] = self.saved_password

    def test_bad_password_exits(self):
        with patch.object(run, "authenticate", side_effect=AuthError()):
            with self.assertRaises(SystemExit) as caught:
                run.main("ada", "how many customers")
        self.assertEqual(caught.exception.code, 1)

    def test_missing_password_without_a_tty_exits(self):
        os.environ.pop("NL2SQL_PASSWORD", None)
        with patch.object(run.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(SystemExit) as caught:
                run.main("ada", "how many customers")
        self.assertIn("NL2SQL_PASSWORD", str(caught.exception))

    def test_tty_prompts_for_the_password(self):
        os.environ.pop("NL2SQL_PASSWORD", None)
        with patch.object(run.sys.stdin, "isatty", return_value=True), \
                patch.object(run.getpass, "getpass", return_value="from-tty"):
            self.assertEqual(run._password(), "from-tty")

    def _patched_main(self, ask, decide=None, answers=None):
        class _Pool:
            def __enter__(self):
                return object()

            def __exit__(self, *args):
                return False

        answers = list(answers or ["approve"])
        with patch.object(run, "authenticate", return_value=Principal("ada", "analyst")), \
                patch.object(run.config, "checkpoint_dsn", return_value="postgresql://checkpoint"), \
                patch.object(run, "ensure_configured"), \
                patch.object(run.limits, "ensure_redis"), \
                patch.object(run.checkpoint, "open_pool", return_value=_Pool()), \
                patch.object(run.checkpoint, "prepare", return_value="saver"), \
                patch.object(run, "build_graph", return_value="graph"), \
                patch.object(run.questions, "ask", side_effect=ask), \
                patch.object(run.questions, "decide", side_effect=decide or (lambda *args, **kwargs: None)), \
                patch("builtins.input", side_effect=answers):
            run.main("ada", "mark order 1 shipped")

    def test_approval_loop_approves_and_prints_the_answer(self):
        pending = questions.Turn(
            thread_id="thread-1",
            status="awaiting_approval",
            trace=["route:write"],
            approval={"kind": "write", "sql": "UPDATE orders SET status = 'x' WHERE id = 1"},
        )
        done = questions.Turn(
            thread_id="thread-1",
            status="answered",
            answer="Done. 1 row(s) affected.",
            trace=["human: approved"],
        )
        seen = {}

        def ask(graph, principal, question, thread_id):
            return pending

        def decide(graph, principal, thread_id, action, sql):
            seen["action"] = action
            seen["sql"] = sql
            return done

        with patch("sys.stdout") as stdout:
            self._patched_main(ask, decide)
        self.assertEqual(seen["action"], "approve")
        self.assertIsNone(seen["sql"])
        printed = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("APPROVAL REQUIRED", printed)
        self.assertIn("Done. 1 row(s) affected.", printed)

    def test_rate_limit_exits(self):
        def ask(*args, **kwargs):
            raise RateLimitExceeded("Rate limit exceeded (30 requests per minute).")

        with self.assertRaises(SystemExit) as caught:
            self._patched_main(ask)
        self.assertEqual(caught.exception.code, 1)


class _Snap:
    def __init__(self, values=None, interrupts=None):
        self.values = values or {}
        self.interrupts = interrupts or []


class _IdleGraph:
    def __init__(self):
        self.values = {}
        self.interrupts = []
        self.invoked = False

    def get_state(self, cfg):
        return _Snap(self.values, self.interrupts)

    def invoke(self, payload, cfg):
        self.invoked = True
        raise AssertionError("graph should not run")


class _Sessions:
    def create_session(self, user_id):
        return "token-1", 60

    def read_session(self, token):
        return "ada" if token == "token-1" else None

    def delete_session(self, token):
        return None


class ApiBranchTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("NL2SQL_MEMORY_DSN", None)
        self.redis = _Redis()
        limits.set_client(self.redis)

    def tearDown(self):
        limits.set_client(None)

    def _app(self, graph=None, readiness=None):
        def authenticator(user_id, password):
            if user_id == "ada" and password == "ada":
                return Principal("ada", "analyst")
            raise AuthError()

        return create_app(
            graph=graph or _IdleGraph(),
            authenticator=authenticator,
            user_loader=lambda user_id: Principal("ada", "analyst"),
            session_store=_Sessions(),
            readiness=readiness,
        )

    def test_missing_bearer_is_unauthorized(self):
        with TestClient(self._app()) as client:
            missing = client.post("/v1/questions", json={"question": "how many customers"})
            blank = client.post(
                "/v1/questions",
                headers={"Authorization": "Bearer "},
                json={"question": "how many customers"},
            )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(blank.status_code, 401)
        self.assertEqual(missing.json()["error"]["code"], "authentication_failed")

    def test_decision_without_an_interrupt_is_not_found(self):
        with TestClient(self._app()) as client:
            token = client.post("/v1/auth/login", json={"user_id": "ada", "password": "ada"}).json()["token"]
            response = client.post(
                "/v1/questions/thread-1/decision",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "approve"},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "thread_not_found")

    def test_pending_thread_does_not_spend_a_rate_limit_token(self):
        class Intr:
            value = {"type": "sql_approval", "kind": "write"}

        idle = _IdleGraph()
        idle.values = {"user_id": "ada", "role": "analyst", "trace": ["route:write"], "question": "mark it"}
        idle.interrupts = [Intr()]
        turn = questions.ask(idle, Principal("ada", "analyst"), "mark it", "thread-1")
        self.assertEqual(turn.status, "awaiting_approval")
        self.assertEqual(self.redis.store, {})
        self.assertFalse(idle.invoked)
        with self.assertRaises(ThreadForbidden):
            questions.ask(idle, Principal("grace", "ops"), "mark it", "thread-1")
        self.assertEqual(self.redis.store, {})

    def test_default_readiness_checks_redis_and_auth(self):
        with patch.object(health.auth, "ping", return_value=True):
            with TestClient(self._app(readiness=None)) as client:
                self.assertEqual(client.get("/health/ready").status_code, 200)
        with patch.object(health.auth, "ping", return_value=False):
            with TestClient(self._app(readiness=None)) as client:
                failed = client.get("/health/ready")
        self.assertEqual(failed.status_code, 503)
        self.redis.fail_ping = True
        with TestClient(self._app(readiness=None)) as client:
            down = client.get("/health/ready")
        self.assertEqual(down.status_code, 503)


class _Redis:
    def __init__(self):
        self.store = {}
        self.fail_ping = False

    def ping(self):
        if self.fail_ping:
            raise RuntimeError("redis down")
        return True

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, ttl):
        return True


class AuthBranchTests(unittest.TestCase):
    def test_ping_and_lookup_failure(self):
        with patch.object(auth, "_connect", side_effect=RuntimeError("down")):
            self.assertFalse(auth.ping())
            with self.assertRaises(AuthError):
                auth.authenticate("ada", "secret")
        with self.assertRaises(AuthError):
            auth.authenticate("ada", "   ")
        with self.assertRaises(AuthError):
            auth.get_user("  ", lookup=lambda user_id: "analyst")

    def test_lookup_exception_is_authentication_failed(self):
        def broken(user_id, password):
            raise OSError("socket")

        with self.assertRaises(AuthError) as caught:
            auth.authenticate("ada", "secret", lookup=broken)
        self.assertEqual(str(caught.exception), "authentication failed")


class CacheBranchTests(unittest.TestCase):
    def setUp(self):
        self.original = cache._client
        cache._client = None
        for key in _LANGCACHE:
            os.environ[key] = "set"

    def tearDown(self):
        cache._client = self.original
        for key in _LANGCACHE:
            os.environ.pop(key, None)

    def test_missing_user_skips_the_client(self):
        class Boom:
            def search(self, **kwargs):
                raise AssertionError("lookup")

            def set(self, **kwargs):
                raise AssertionError("store")

        cache._client = Boom()
        self.assertIsNone(cache.search("how many", "analyst", None))
        self.assertIsNone(cache.search("how many", "analyst", "  "))
        cache.store("how many", "Ada", "analyst", None)
        cache.store("how many", "", "analyst", "ada")

    def test_client_errors_do_not_raise(self):
        class Boom:
            def search(self, **kwargs):
                raise RuntimeError("down")

            def set(self, **kwargs):
                raise RuntimeError("down")

        cache._client = Boom()
        self.assertIsNone(cache.search("how many", "analyst", "ada"))
        cache.store("how many", "Ada", "analyst", "ada")

    def test_hit_and_miss(self):
        class Item:
            response = "cached customers"

        class Miss:
            data = []

        class Hit:
            data = [Item()]

        class Client:
            def __init__(self):
                self.found = Miss()

            def search(self, **kwargs):
                return self.found

        client = Client()
        cache._client = client
        self.assertIsNone(cache.search("how many", "analyst", "ada"))
        client.found = Hit()
        self.assertEqual(cache.search("how many", "analyst", "ada"), "cached customers")


class PureGapTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_EMBED_MODEL", "NL2SQL_EMBED_DIMS",
            "OLLAMA_BASE_URL", "NVIDIA_API_KEY", "NL2SQL_MEMORY_DSN", "NL2SQL_AUTH_DSN",
            "REDIS_URL", "LLM_PROVIDER", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
            "NL2SQL_ANALYST_DSN",
        ):
            os.environ.pop(key, None)
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
        limits.set_client(None)
        from nl2sql.llm import registry as llm_registry
        from nl2sql.services import memory

        llm_registry.reset()
        memory._memory = None
        memory._embedder_warned = False

    def test_memory_configuration_and_sync_wrappers(self):
        import asyncio

        from nl2sql.llm import registry as llm_registry
        from nl2sql.services import memory

        os.environ["NL2SQL_EMBED_DIMS"] = "32"
        os.environ["OPENAI_API_KEY"] = "sk"
        os.environ["OPENAI_BASE_URL"] = "https://example.test"
        openai_embed = memory.embedder_config()
        self.assertEqual(openai_embed["provider"], "openai")
        self.assertEqual(openai_embed["config"]["embedding_dims"], 32)

        os.environ.pop("OPENAI_API_KEY")
        os.environ.pop("NL2SQL_EMBED_DIMS")
        os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434/v1"
        ollama_embed = memory.embedder_config()
        self.assertEqual(ollama_embed["provider"], "ollama")
        self.assertEqual(memory._dims("missing-model"), 1536)

        os.environ.pop("OLLAMA_BASE_URL")
        os.environ["NVIDIA_API_KEY"] = "nv"
        self.assertEqual(memory.embedder_config()["config"]["model"], config.nvidia_embed_model())
        os.environ.pop("NVIDIA_API_KEY")
        self.assertIsNone(memory.embedder_config())
        memory._warn_missing_embedder()
        memory._warn_missing_embedder()

        class Entry:
            def __init__(self, provider, name):
                self.provider = provider
                self.name = name

        for provider in ("groq", "ollama", "nvidia", "openai"):
            with patch.object(memory.registry, "entries", return_value=[Entry(provider, "model-1")]), \
                    patch.object(memory.registry, "start_index", return_value=0):
                built = memory._llm_config()
            self.assertEqual(built["config"]["model"], "model-1")

        os.environ["NL2SQL_MEMORY_DSN"] = "postgresql://memory"
        os.environ["OPENAI_API_KEY"] = "sk"
        self.assertEqual(memory._rows({"results": [{"memory": "x"}]}), [{"memory": "x"}])
        self.assertEqual(memory._rows(None), [])

        class FakeMemory:
            def __init__(self):
                self.added = None

            async def search(self, query, filters=None, top_k=5):
                return {"results": [{"memory": "likes widgets"}, "plain", {"memory": ""}]}

            async def add(self, messages, user_id=None):
                self.added = (messages, user_id)

        fake = FakeMemory()
        import mem0

        memory._memory = None
        with patch.object(memory, "_llm_config", return_value={"provider": "openai", "config": {"model": "m"}}), \
                patch.object(mem0.AsyncMemory, "from_config", return_value=fake):
            text = asyncio.run(memory._search("ada", "revenue"))
            asyncio.run(memory._add("ada", "q", "a"))
        self.assertIn("* likes widgets", text)
        self.assertIn("* plain", text)
        self.assertEqual(fake.added[1], "ada")

        with patch.object(memory, "_search", side_effect=RuntimeError("down")):
            self.assertEqual(memory.search_sync("ada", "revenue"), "")
        with patch.object(memory, "_add", side_effect=RuntimeError("down")):
            memory.add_sync("ada", "q", "a")
        with patch.object(memory, "_add", return_value=None):
            memory.add_sync("ada", "q", "stored")

        llm_registry.ollama_host("http://localhost:11434/v1")
        os.environ["LLM_PROVIDER"] = "groq"
        self.assertEqual(llm_registry.start_index([Entry("groq", "g")]), 0)

    def test_auth_sql_and_checkpoint_pool(self):
        class _Cursor:
            def __init__(self, row):
                self.row = row
                self.sql = ""

            def execute(self, sql, params=None):
                self.sql = sql

            def fetchone(self):
                return self.row

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Connection:
            def __init__(self, row):
                self.cursor_obj = _Cursor(row)

            def cursor(self):
                return self.cursor_obj

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        os.environ["NL2SQL_AUTH_DSN"] = "postgresql://auth"
        with patch.object(auth, "_connect", return_value=_Connection(("analyst",))):
            self.assertEqual(auth._lookup_password("ada", "ada"), "analyst")
            self.assertEqual(auth._lookup_user("ada"), "analyst")
            self.assertTrue(auth.ping())
        with patch.object(auth, "_connect", return_value=_Connection(None)):
            self.assertIsNone(auth._lookup_password("ada", "nope"))
        os.environ["NL2SQL_AUTH_DSN"] = ""
        with self.assertRaises(RuntimeError):
            auth._connect()
        def reject(user_id, password):
            raise AuthError()

        with self.assertRaises(AuthError):
            auth.authenticate("ada", "secret", lookup=reject)

        os.environ["NL2SQL_CHECKPOINT_DSN"] = "postgresql://checkpoint"
        with patch("nl2sql.services.checkpoint.ConnectionPool") as pool:
            checkpoint.open_pool()
        pool.assert_called_once()
        with patch("nl2sql.services.checkpoint.PostgresSaver") as saver:
            checkpoint.prepare(object())
        saver.return_value.setup.assert_called_once()
        os.environ["NL2SQL_ANALYST_DSN"] = ""
        self.assertIn("nl2sql_analyst", config.role_dsn("analyst"))

    def test_cost_prompt_limits_and_trace_flush(self):
        from nl2sql.llm import cost, prompts
        from nl2sql.services import sessions

        class Block:
            text = "c"

        class Message:
            content = [{"text": "a"}, "b", Block(), object()]

        self.assertEqual(cost.message_text(Message()), "abc")

        class Dump:
            content = 5

            def model_dump_json(self):
                return "{\"x\": 1}"

        self.assertEqual(cost.message_text(Dump()), "{\"x\": 1}")
        self.assertIn("hello", cost.prompt_text([("human", "hello"), {"content": "there"}, Dump()]))

        class Measured:
            usage_metadata = {"input_tokens": 3, "output_tokens": 4}

        self.assertEqual(cost.usage(Measured(), [("human", "hi")], "gpt-5.4-mini"), (3, 4))

        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk"
        os.environ["LANGFUSE_TRACING_ENABLED"] = "true"
        with patch("langfuse.get_client", side_effect=RuntimeError("down")):
            loaded = prompts.load("nl2sql-route", prompts.ROUTE, {"memory": "note", "question": "hi"})
            llm_service.flush_traces()
        self.assertIn("note", loaded.text)
        self.assertIsNone(loaded.client)

        self.assertIsNone(sessions.read_session("  "))
        sessions.delete_session("")
        self.assertEqual(llm_service.user_bucket({}), "anonymous")
        self.assertEqual(llm_service._ascii("café", 2), "ca")

        fake = _Redis()
        limits.set_client(None)
        os.environ["REDIS_URL"] = ""
        with self.assertRaises(RuntimeError):
            limits.client()
        os.environ["REDIS_URL"] = "redis://localhost:6379/0"
        with patch("redis.Redis.from_url", return_value=fake):
            limits.ensure_redis()
            limits.ensure_redis()
        limits.reserve("ada", 0)
        limits.reconcile("ada", 1.0, 1.0)

        import asyncio

        async def time_out(coro, timeout):
            coro.close()
            raise TimeoutError()

        with patch.object(llm_service.registry, "ensure_configured"), \
                patch.object(llm_service.limits, "ensure_redis"), \
                patch.object(llm_service.asyncio, "wait_for", time_out):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(llm_service.llm_service.call([("human", "hi")]))
        self.assertIn("timed out", str(caught.exception))


class YamlConfigTests(unittest.TestCase):
    def setUp(self):
        graph._schema_cache.clear()

    def tearDown(self):
        graph._schema_cache.clear()

    def test_committed_roles_discover_tables_and_keep_write_privilege(self):
        analyst = config.ROLES["analyst"]
        ops = config.ROLES["ops"]
        self.assertFalse(analyst.can_write)
        self.assertEqual(analyst.allowed_tables, frozenset())
        self.assertEqual(analyst.denied_columns, frozenset())
        self.assertTrue(ops.can_write)
        self.assertEqual(ops.allowed_tables, frozenset())

    def test_env_overrides_the_yaml_rate_limit(self):
        saved = os.environ.get("RATE_LIMIT_PER_MINUTE")
        os.environ.pop("RATE_LIMIT_PER_MINUTE", None)
        try:
            self.assertEqual(config.rate_limit_per_minute(), 30)
            os.environ["RATE_LIMIT_PER_MINUTE"] = "7"
            self.assertEqual(config.rate_limit_per_minute(), 7)
        finally:
            if saved is None:
                os.environ.pop("RATE_LIMIT_PER_MINUTE", None)
            else:
                os.environ["RATE_LIMIT_PER_MINUTE"] = saved

    def test_missing_config_file_raises(self):
        with self.assertRaises(RuntimeError) as caught:
            config.load_file(Path("/no/such/config.yaml"))
        self.assertIn("Missing config file", str(caught.exception))

    def test_open_role_loads_every_visible_public_table(self):
        seen = []

        def run(mode, sql, max_rows=500, dsn=None):
            seen.append(sql)
            return _schema_rows()

        with patch.object(sandbox, "run", run):
            loaded = graph.load_schema_node({"role": "analyst"})
        self.assertNotIn("table_name IN", seen[0])
        self.assertIn("table_schema='public'", seen[0])
        self.assertIn("customers", loaded["schema"])
        denied = graph.guard_node({"role": "analyst", "sql": "SELECT id FROM inventory", "attempts": 0})
        self.assertIn("not permitted", denied.update["error"])
        allowed = graph.guard_node({"role": "analyst", "sql": "SELECT id FROM customers", "attempts": 0})
        self.assertEqual(allowed.goto, "preflight")

    def test_listed_tables_stay_an_extra_filter(self):
        shop = config.Policy("analyst", frozenset({"customers", "orders"}))
        seen = []

        def run(mode, sql, max_rows=500, dsn=None):
            seen.append(sql)
            return _schema_rows()

        with patch.dict(config.ROLES, {"analyst": shop}), patch.object(sandbox, "run", run):
            graph.load_schema_node({"role": "analyst"})
        self.assertIn("table_name IN ('customers', 'orders')", seen[0])


class CheckpointBranchTests(unittest.TestCase):
    def test_open_pool_requires_a_dsn(self):
        saved = os.environ.get("NL2SQL_CHECKPOINT_DSN")
        os.environ["NL2SQL_CHECKPOINT_DSN"] = ""
        try:
            with self.assertRaises(RuntimeError) as caught:
                checkpoint.open_pool()
            self.assertIn("NL2SQL_CHECKPOINT_DSN", str(caught.exception))
        finally:
            if saved is None:
                os.environ.pop("NL2SQL_CHECKPOINT_DSN", None)
            else:
                os.environ["NL2SQL_CHECKPOINT_DSN"] = saved


def _inline_context(start):
    class _Process:
        def __init__(self, target, args):
            self.target = target
            self.args = args

        def start(self):
            if start is None:
                return None
            self.target(*self.args)

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

        def kill(self):
            return None

    class _Context:
        def Queue(self):
            return queue.Queue()

        def Process(self, target, args, daemon=True):
            return _Process(target, args)

    return _Context()
