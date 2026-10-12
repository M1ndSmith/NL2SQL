"""Execution accuracy, schema evidence, candidate agreement, audit, and answer checks."""
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from nl2sql import config  # noqa: E402
from nl2sql.agent.evidence import catalog_kind, collect, load_catalog, prompt_for  # noqa: E402
from nl2sql.agent.redact import redact  # noqa: E402
from nl2sql.eval.accuracy import load_cases, matches, same_rows, score_query  # noqa: E402
from nl2sql.eval.answer import grounded, grounded_reply  # noqa: E402
from nl2sql.services import audit  # noqa: E402
import nl2sql.agent.graph as graph  # noqa: E402
import nl2sql.agent.sandbox as sandbox  # noqa: E402
import nl2sql.llm.service as llm_service  # noqa: E402


def _columns():
    return {
        "columns": ["table_name", "column_name", "data_type"],
        "rows": [
            ["customers", "id", "integer"],
            ["customers", "name", "text"],
            ["customers", "email", "text"],
            ["orders", "id", "integer"],
        ],
    }


class AccuracyTests(unittest.TestCase):
    def test_rows_match_without_regard_to_order_or_sql_text(self):
        self.assertTrue(same_rows(["b", "a"], [[2, 1], [4, 3]], ["a", "b"], [[3, 4], [1, 2]]))
        self.assertTrue(same_rows(["n"], [[None]], ["n"], [[None]]))
        self.assertFalse(same_rows(["n"], [[None]], ["n"], [[""]]))
        self.assertTrue(same_rows(["n"], [[10]], ["n"], [[Decimal("10.00")]]))
        self.assertFalse(same_rows(["n"], [[1], [1]], ["n"], [[1]]))

    def test_any_accepted_shape_can_pass(self):
        accepted = [
            {"columns": ["name"], "rows": [["Ada"], ["Grace"]]},
            {"columns": ["id", "name"], "rows": [[1, "Ada"], [2, "Grace"]]},
        ]
        self.assertTrue(matches(["id", "name"], [[2, "Grace"], [1, "Ada"]], accepted))
        self.assertFalse(matches(["name"], [["Ada"]], accepted))

    def test_golden_cases_score_their_own_rows_and_reject_a_wrong_set(self):
        cases = load_cases()
        self.assertGreaterEqual(len(cases), 4)
        for case in cases:
            first = case["accepted"][0]
            self.assertTrue(matches(first["columns"], first["rows"], case["accepted"]))
            self.assertFalse(matches(["x"], [[-1]], case["accepted"]))

    def test_score_query_runs_the_sql(self):
        seen = []

        def run(mode, sql, max_rows=500, dsn=None):
            seen.append((mode, sql))
            return {"columns": ["count"], "rows": [[2]]}

        accepted = [{"columns": ["count"], "rows": [[2]]}]
        self.assertTrue(score_query("SELECT count(*) AS count FROM customers", accepted, run))
        self.assertEqual(seen[0][0], "read")
        self.assertIn("count(*)", seen[0][1])
        self.assertFalse(score_query("SELECT 0", accepted, lambda *args, **kwargs: {"columns": ["count"], "rows": [[0]]}))


class RedactTests(unittest.TestCase):
    def test_email_and_separated_phone_are_replaced(self):
        self.assertEqual(redact("ada@shop.test"), "[redacted]")
        self.assertEqual(redact("(415) 555-0100"), "[redacted]")
        self.assertEqual(redact("+1 415 555 0100"), "[redacted]")
        self.assertEqual(redact("415-555-0100"), "[redacted]")

    def test_years_ids_and_dates_stay(self):
        self.assertEqual(redact("2024"), "2024")
        self.assertEqual(redact("1001"), "1001")
        self.assertEqual(redact("2024-01-02"), "2024-01-02")
        self.assertEqual(redact("Ada ada@shop.test"), "Ada [redacted]")


class EvidenceTests(unittest.TestCase):
    def test_catalog_marker_does_not_match_user_sql(self):
        self.assertIsNone(catalog_kind("SELECT DISTINCT id FROM customers"))

    def test_open_role_loads_keys_and_values_and_skips_denied_columns(self):
        seen = []

        def run(mode, sql, max_rows=500, dsn=None):
            seen.append(sql)
            kind = catalog_kind(sql)
            if kind == "foreign_keys":
                return {
                    "columns": ["table_name", "column_name", "foreign_table", "foreign_column"],
                    "rows": [["orders", "customer_id", "customers", "id"]],
                }
            if kind == "values":
                return {"columns": ["value"], "rows": [["Ada"]]}
            return _columns()

        policy = config.Policy(
            "analyst",
            frozenset(),
            denied_columns=frozenset({"customers.email"}),
        )
        text, visible = collect(run, policy)
        self.assertNotIn("table_name IN", seen[0])
        self.assertNotIn("email", text)
        self.assertIn("orders.customer_id -> customers.id", text)
        self.assertIn("Ada", text)
        self.assertEqual(visible, frozenset({"customers", "orders"}))
        distinct = [sql for sql in seen if catalog_kind(sql) == "values"]
        self.assertTrue(distinct)
        self.assertTrue(all('"email"' not in sql for sql in distinct))

    def test_table_cap_keeps_a_sorted_subset(self):
        def run(mode, sql, max_rows=500, dsn=None):
            kind = catalog_kind(sql)
            if kind == "foreign_keys":
                return {"columns": ["a", "b", "c", "d"], "rows": []}
            if kind == "values":
                return {"columns": ["value"], "rows": []}
            return _columns()

        policy = config.Policy("analyst", frozenset())
        with patch.object(config, "schema_table_cap", lambda: 1):
            text, visible = collect(run, policy)
        self.assertEqual(visible, frozenset({"customers", "orders"}))
        self.assertIn("customers(", text)
        self.assertNotIn("orders(", text)

    def test_named_table_is_ranked_and_the_allowlist_stays_full(self):
        def run(mode, sql, max_rows=500, dsn=None):
            kind = catalog_kind(sql)
            if kind == "foreign_keys":
                return {
                    "columns": ["table_name", "column_name", "foreign_table", "foreign_column"],
                    "rows": [["orders", "customer_id", "customers", "id"]],
                }
            if kind == "values":
                return {"columns": ["value"], "rows": []}
            return _columns()

        policy = config.Policy("analyst", frozenset())
        with patch.object(config, "schema_table_cap", lambda: 1):
            text, visible = collect(run, policy, "how many orders")
        self.assertEqual(visible, frozenset({"customers", "orders"}))
        self.assertIn("orders(", text)
        self.assertNotIn("customers(", text)

    def test_email_sample_is_dropped_and_a_name_stays(self):
        def run(mode, sql, max_rows=500, dsn=None):
            kind = catalog_kind(sql)
            if kind == "foreign_keys":
                return {"columns": ["a", "b", "c", "d"], "rows": []}
            if kind == "values" and '"email"' in sql:
                return {"columns": ["value"], "rows": [["ada@shop.test"]]}
            if kind == "values":
                return {"columns": ["value"], "rows": [["Ada"]]}
            return _columns()

        text, _visible = collect(run, config.Policy("analyst", frozenset()))
        self.assertNotIn("ada@shop.test", text)
        self.assertIn("Ada", text)
        self.assertIn("email text", text)

    def test_second_question_samples_only_the_new_table(self):
        seen = []

        def run(mode, sql, max_rows=500, dsn=None):
            kind = catalog_kind(sql)
            if kind == "values":
                seen.append(sql)
                return {"columns": ["value"], "rows": [["Ada"]]}
            if kind == "foreign_keys":
                return {"columns": ["a", "b", "c", "d"], "rows": []}
            return {
                "columns": ["table_name", "column_name", "data_type"],
                "rows": [
                    ["customers", "name", "text"],
                    ["orders", "note", "text"],
                ],
            }

        policy = config.Policy("analyst", frozenset())
        catalog = load_catalog(run, policy)
        with patch.object(config, "schema_table_cap", lambda: 1):
            prompt_for(run, policy, catalog, "how many customers")
            first = list(seen)
            prompt_for(run, policy, catalog, "how many orders")
        self.assertEqual(len(first), 1)
        self.assertIn('"name"', first[0])
        self.assertEqual(len(seen), 2)
        self.assertIn('"note"', seen[1])


class CandidateTests(unittest.TestCase):
    def test_different_results_go_back_to_generate(self):
        def run(mode, sql, max_rows=500, dsn=None):
            if "SELECT 2" in sql:
                return {"columns": ["n"], "rows": [[2]]}
            return {"columns": ["n"], "rows": [[1]]}

        with patch.object(sandbox, "run", run):
            command = graph.agree_node({
                "role": "analyst",
                "kind": "read",
                "sql": "SELECT 1 AS n",
                "alternate": "SELECT 2 AS n",
                "attempts": 0,
                "cost": 1,
            })
        self.assertEqual(command.goto, "generate")
        self.assertIn("different", command.update["error"])

    def test_matching_results_continue_and_a_missing_alternate_uses_cost(self):
        def run(mode, sql, max_rows=500, dsn=None):
            return {"columns": ["n"], "rows": [[1]]}

        with patch.object(sandbox, "run", run):
            agreed = graph.agree_node({
                "role": "analyst",
                "kind": "read",
                "sql": "SELECT 1 AS n",
                "alternate": "SELECT 1 AS n",
                "attempts": 0,
                "cost": 1,
            })
        self.assertEqual(agreed.goto, "execute")
        self.assertIn("candidates agree", agreed.update["trace"])
        expensive = graph.agree_node({
            "role": "analyst",
            "kind": "read",
            "sql": "SELECT 1 AS n",
            "alternate": "",
            "cost": config.COST_APPROVAL_THRESHOLD + 1,
        })
        self.assertEqual(expensive.goto, "approval")

    def test_unsafe_alternate_is_not_run(self):
        called = []

        def run(mode, sql, max_rows=500, dsn=None):
            called.append(sql)
            return {"columns": ["n"], "rows": [[1]]}

        with patch.object(sandbox, "run", run):
            command = graph.agree_node({
                "role": "analyst",
                "kind": "read",
                "sql": "SELECT 1 AS n",
                "alternate": "DROP TABLE orders",
                "attempts": 0,
                "cost": 1,
            })
        self.assertEqual(command.goto, "generate")
        self.assertIn("guardrail", command.update["error"])
        self.assertEqual(called, [])


class AuditTests(unittest.TestCase):
    def test_missing_dsn_does_not_connect(self):
        called = []
        with patch.object(config, "audit_dsn", lambda: ""):
            audit.record("ada", "analyst", "SELECT 1", 1, "executed", connect=lambda dsn: called.append(dsn))
        self.assertEqual(called, [])

    def test_insert_is_append_only(self):
        class _Cursor:
            def __init__(self):
                self.sql = ""
                self.params = None

            def execute(self, sql, params):
                self.sql = sql
                self.params = params

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Conn:
            def __init__(self):
                self.cur = _Cursor()
                self.committed = False

            def cursor(self):
                return self.cur

            def commit(self):
                self.committed = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        conn = _Conn()
        with patch.object(config, "audit_dsn", lambda: "postgresql://nl2sql_audit:pw@localhost/audit"):
            audit.record("ada", "analyst", "SELECT 1", 1, "executed", connect=lambda dsn: conn)
        self.assertIn("INSERT INTO events", conn.cur.sql)
        self.assertNotIn("UPDATE", conn.cur.sql)
        self.assertNotIn("DELETE", conn.cur.sql)
        self.assertEqual(conn.cur.params[0], "ada")
        self.assertTrue(conn.committed)

        def down(dsn):
            raise OSError("down")

        with patch.object(config, "audit_dsn", lambda: "postgresql://nl2sql_audit:pw@localhost/audit"):
            audit.record("ada", "analyst", "SELECT 1", 1, "executed", connect=down)

    def test_list_is_scoped_and_read_only(self):
        from datetime import datetime, timezone

        class _Cursor:
            def __init__(self):
                self.sql = ""
                self.params = None

            def execute(self, sql, params):
                self.sql = sql
                self.params = params

            def fetchall(self):
                return [(datetime(2026, 10, 12, tzinfo=timezone.utc), "SELECT 1", 1, "executed")]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Conn:
            def __init__(self):
                self.cur = _Cursor()

            def cursor(self):
                return self.cur

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        conn = _Conn()
        with patch.object(config, "audit_dsn", lambda: ""):
            self.assertEqual(audit.list_for("ada", connect=lambda dsn: conn), [])
        self.assertEqual(conn.cur.sql, "")
        with patch.object(config, "audit_dsn", lambda: "postgresql://nl2sql_audit:pw@localhost/audit"):
            found = audit.list_for("ada", connect=lambda dsn: conn)
        self.assertIn("SELECT created_at, sql, rowcount, decision", conn.cur.sql)
        self.assertIn("WHERE user_id = %s", conn.cur.sql)
        self.assertNotIn("UPDATE", conn.cur.sql)
        self.assertNotIn("DELETE", conn.cur.sql)
        self.assertEqual(conn.cur.params, ("ada",))
        self.assertEqual(found[0]["sql"], "SELECT 1")
        self.assertTrue(found[0]["created_at"].startswith("2026-10-12"))

    def test_sql_roles_are_not_granted_the_audit_table(self):
        text = (ROOT / "docker" / "postgres" / "init.sh").read_text()
        self.assertIn("GRANT INSERT ON events TO nl2sql_audit", text)
        self.assertIn("GRANT SELECT ON events TO nl2sql_audit", text)
        self.assertNotIn("GRANT UPDATE ON events", text)
        self.assertNotIn("GRANT DELETE ON events", text)
        self.assertIn("GRANT CONNECT ON DATABASE audit TO nl2sql_audit", text)
        self.assertNotIn("GRANT CONNECT ON DATABASE audit TO nl2sql_analyst", text)
        self.assertNotIn("GRANT CONNECT ON DATABASE audit TO nl2sql_ops", text)

    def test_execute_records_the_decision(self):
        recorded = []

        def run(mode, sql, max_rows=500, dsn=None):
            return {"columns": ["id"], "rows": [[7]]}

        with patch.object(sandbox, "run", run), patch.object(graph.audit, "record", lambda *args, **kwargs: recorded.append(args)):
            graph.execute_node({
                "role": "analyst",
                "kind": "read",
                "sql": "SELECT 1",
                "user_id": "ada",
                "trace": ["human: approved"],
            })
        self.assertEqual(recorded[0][0], "ada")
        self.assertEqual(recorded[0][1], "analyst")
        self.assertEqual(recorded[0][3], 1)
        self.assertEqual(recorded[0][4], "approved")


class AnswerTests(unittest.TestCase):
    def test_numbers_must_come_from_the_rows(self):
        self.assertFalse(grounded("There are 9 customers", [["Ada"], ["Grace"]]))
        self.assertTrue(grounded("There are 2 customers", [[2]]))
        self.assertTrue(grounded("Ada and Grace", [["Ada"], ["Grace"]]))
        reply = grounded_reply(["name"], [["Ada"], ["Grace"]])
        self.assertIn("Ada", reply)
        self.assertNotIn("9", reply)

    def test_respond_replaces_an_invented_number(self):
        def call(messages, response_format=None, **kwargs):
            return type("Msg", (), {"content": "There are 9 customers"})()

        with patch.object(llm_service.llm_service, "call_sync", call):
            out = graph.respond_node({
                "kind": "read",
                "question": "who are the customers",
                "sql": "SELECT name FROM customers",
                "columns": ["name"],
                "rows": [["Ada"], ["Grace"]],
                "role": "analyst",
                "user_id": "ada",
            }, {})
        self.assertIn("answer rejected invented number", out["trace"])
        self.assertIn("Ada", out["answer"])
        self.assertNotIn("9", out["answer"])
