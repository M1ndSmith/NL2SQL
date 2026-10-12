"""Deterministic guardrails + permission enforcement on the SQL AST.

The LLM never gets to decide what is allowed. Everything here is code.
"""
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ..config import Policy

DIALECT = "postgres"


class GuardrailError(ValueError):
    """Message is fed back to the generator for repair, so keep it actionable."""


READ_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)
WRITE_TYPES = (exp.Insert, exp.Update, exp.Delete)

BLOCKED_FUNCS = {
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "dblink_exec", "set_config", "current_setting",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_advisory_lock",
    "query_to_xml", "xpath", "copy",
}


@dataclass
class Checked:
    kind: str          # "read" | "write"
    sql: str           # normalized, and LIMIT-wrapped for reads
    tables: set[str]


def check(sql: str, policy: Policy) -> Checked:
    try:
        stmts = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as e:
        raise GuardrailError(f"SQL does not parse: {str(e)[:200]}")
    if len(stmts) != 1:
        raise GuardrailError("Exactly one SQL statement is allowed.")
    tree = stmts[0]

    # 1. Statement allowlist (not a denylist: anything unknown is rejected)
    if isinstance(tree, READ_TYPES):
        kind = "read"
        if tree.find(*WRITE_TYPES):
            raise GuardrailError("Data-modifying statements nested in a read query are not allowed.")
    elif isinstance(tree, WRITE_TYPES):
        kind = "write"
        if not policy.can_write:
            raise GuardrailError(f"Role '{policy.role}' is not permitted to write.")
        if isinstance(tree, (exp.Update, exp.Delete)) and not tree.args.get("where"):
            raise GuardrailError("UPDATE/DELETE without a WHERE clause is not allowed.")
    else:
        raise GuardrailError(f"Statement type {type(tree).__name__} is not allowed.")

    # 2. Table permissions (CTE names are not real tables)
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    tables: set[str] = set()
    for t in tree.find_all(exp.Table):
        if t.name in ctes:
            continue
        if t.db and t.db.lower() != "public":
            raise GuardrailError(f"Schema '{t.db}' is not accessible.")
        if t.name not in policy.allowed_tables:
            raise GuardrailError(
                f"Table '{t.name}' is not permitted. Allowed: {sorted(policy.allowed_tables)}"
            )
        tables.add(t.name)

    # 3. Column permissions (conservative: match by column name across tables used)
    denied = {d.split(".", 1)[1] for d in policy.denied_columns if d.split(".", 1)[0] in tables}
    if denied:
        if any(c.name in denied for c in tree.find_all(exp.Column)):
            raise GuardrailError(f"Columns {sorted(denied)} are restricted; do not reference them.")
        if any(not isinstance(s.parent, exp.Count) for s in tree.find_all(exp.Star)):
            raise GuardrailError("SELECT * is not allowed on tables with restricted columns; list columns.")

    # 4. Function blocklist
    for f in tree.find_all(exp.Func):
        name = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
        if name in BLOCKED_FUNCS:
            raise GuardrailError(f"Function '{name}' is not allowed.")

    out = tree.sql(dialect=DIALECT)
    if kind == "read":  # 5. Row cap enforced by wrapping, so no LIMIT trick can bypass it
        out = f"SELECT * FROM ({out}) AS _q LIMIT {policy.max_rows}"
    return Checked(kind, out, tables)
