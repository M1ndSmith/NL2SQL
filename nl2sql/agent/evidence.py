"""Schema text for the generator: columns, foreign keys, and a few real values."""
import re
from dataclasses import dataclass, field

from .. import config
from .redact import redact

CATALOG_PREFIX = "-- nl2sql-catalog\n"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEXT = {"text", "character varying", "varchar", "character", "citext"}


@dataclass
class Catalog:
    columns: dict[str, list[str]]
    edges: list[tuple[str, str, str, str]]
    visible: frozenset[str]
    text_columns: list[tuple[str, str]]
    samples: dict[tuple[str, str], list[str]] = field(default_factory=dict)


def catalog_kind(sql: str) -> str | None:
    if not sql.startswith(CATALOG_PREFIX):
        return None
    if "table_constraints" in sql:
        return "foreign_keys"
    if "select distinct" in sql.lower():
        return "values"
    return "columns"


def _quote(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(name)
    return '"' + name + '"'


def _named(name: str, question: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
        question or "",
        re.IGNORECASE,
    ) is not None


def _keep_sample(value: str) -> str | None:
    cleaned = redact(value)
    if cleaned.replace("[redacted]", "").strip():
        return cleaned
    return None


def load_catalog(run, policy) -> Catalog:
    """Read columns and foreign keys through the role login. Denied columns are omitted."""
    columns = run(
        "read",
        CATALOG_PREFIX
        + "SELECT table_name, column_name, data_type FROM information_schema.columns "
        + "WHERE table_schema='public'"
        + f"{config.schema_name_filter(policy.allowed_tables)} ORDER BY table_name, ordinal_position",
        max_rows=2000,
        dsn=config.role_dsn(policy.role),
    )
    by_table: dict[str, list[str]] = {}
    text_columns: list[tuple[str, str]] = []
    for row in columns["rows"]:
        table_name, column_name, data_type = row[0], row[1], row[2]
        if f"{table_name}.{column_name}" in policy.denied_columns:
            continue
        by_table.setdefault(table_name, []).append(f"{column_name} {data_type}")
        if str(data_type).lower() in _TEXT and _IDENT.fullmatch(table_name) and _IDENT.fullmatch(column_name):
            text_columns.append((table_name, column_name))

    keys = run(
        "read",
        CATALOG_PREFIX
        + "SELECT kcu.table_name, kcu.column_name, ccu.table_name, ccu.column_name "
        + "FROM information_schema.table_constraints AS tc "
        + "JOIN information_schema.key_column_usage AS kcu "
        + "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        + "JOIN information_schema.constraint_column_usage AS ccu "
        + "ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
        + "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'",
        max_rows=2000,
        dsn=config.role_dsn(policy.role),
    )
    edges = []
    for row in keys["rows"]:
        if len(row) < 4:
            continue
        table_name, column_name, foreign_table, foreign_column = row[0], row[1], row[2], row[3]
        if table_name not in by_table or foreign_table not in by_table:
            continue
        if f"{table_name}.{column_name}" in policy.denied_columns:
            continue
        if f"{foreign_table}.{foreign_column}" in policy.denied_columns:
            continue
        edges.append((table_name, column_name, foreign_table, foreign_column))
    return Catalog(by_table, edges, frozenset(by_table), text_columns)


def rank_tables(catalog: Catalog, question: str, cap: int) -> list[str]:
    """Cap 0 keeps every table, sorted by name. A positive cap ranks, then stops."""
    names = sorted(catalog.columns)
    if cap <= 0 or cap >= len(names):
        return names
    named = [name for name in names if _named(name, question)]
    named_set = set(named)
    hop = sorted({
        other
        for table_name, _column, foreign_table, _foreign_column in catalog.edges
        for other in (
            [foreign_table] if table_name in named_set and foreign_table not in named_set
            else [table_name] if foreign_table in named_set and table_name not in named_set
            else []
        )
    })
    chosen = set(named) | set(hop)
    rest = [name for name in names if name not in chosen]
    return (named + hop + rest)[:cap]


def _fill_samples(run, policy, catalog: Catalog, tables: list[str]) -> None:
    value_cap = config.distinct_value_cap()
    if value_cap <= 0:
        return
    wanted = set(tables)
    for table_name, column_name in catalog.text_columns:
        key = (table_name, column_name)
        if table_name not in wanted or key in catalog.samples:
            continue
        sampled = run(
            "read",
            CATALOG_PREFIX
            + f"SELECT DISTINCT {_quote(column_name)} AS value FROM public.{_quote(table_name)} "
            + f"WHERE {_quote(column_name)} IS NOT NULL ORDER BY 1 LIMIT {int(value_cap)}",
            max_rows=value_cap,
            dsn=config.role_dsn(policy.role),
        )
        values = []
        for row in sampled["rows"]:
            if not row or row[0] is None:
                continue
            kept = _keep_sample(str(row[0]))
            if kept is not None:
                values.append(kept)
        catalog.samples[key] = values


def _render(catalog: Catalog, tables: list[str]) -> str:
    shown = set(tables)
    lines = []
    for table_name in tables:
        lines.append(f"{table_name}({', '.join(catalog.columns[table_name])})")
        for edge in sorted(catalog.edges, key=lambda item: (item[0], item[1], item[2], item[3])):
            source, column_name, foreign_table, foreign_column = edge
            if source == table_name and foreign_table in shown:
                lines.append(f"  {source}.{column_name} -> {foreign_table}.{foreign_column}")
        for (sample_table, sample_column), values in sorted(catalog.samples.items()):
            if sample_table == table_name and values:
                lines.append(f"  values {sample_column}: {' | '.join(values)}")
    return "\n".join(lines)


def prompt_for(run, policy, catalog: Catalog, question: str) -> str:
    tables = rank_tables(catalog, question, config.schema_table_cap())
    _fill_samples(run, policy, catalog, tables)
    return _render(catalog, tables)


def collect(run, policy, question: str = "") -> tuple[str, frozenset[str]]:
    """Read the catalog through the role login and render the prompt for this question."""
    catalog = load_catalog(run, policy)
    return prompt_for(run, policy, catalog, question), catalog.visible
