"""Compare query results as unordered bags of rows. NULL is not an empty string."""
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

_GOLDEN = Path(__file__).resolve().parents[2] / "evals" / "shop.yaml"


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    return None


def _cell(value):
    if value is None:
        return ("null",)
    number = _number(value)
    if number is not None:
        return ("number", number)
    return ("text", str(value))


def _bag(columns, rows) -> tuple:
    names = [str(name) for name in columns]
    order = sorted(range(len(names)), key=lambda index: names[index])
    ordered_names = tuple(names[index] for index in order)
    items = []
    for row in rows:
        items.append(tuple(_cell(row[index]) for index in order))
    return (ordered_names, tuple(sorted(items)))


def same_rows(left_columns, left_rows, right_columns, right_rows) -> bool:
    return _bag(left_columns, left_rows) == _bag(right_columns, right_rows)


def matches(columns, rows, accepted: list) -> bool:
    return any(same_rows(columns, rows, spec["columns"], spec["rows"]) for spec in accepted)


def load_cases(path: Path | None = None) -> list[dict]:
    loaded = yaml.safe_load((path or _GOLDEN).read_text())
    cases = loaded.get("cases") if isinstance(loaded, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("golden file has no cases")
    for case in cases:
        if not case.get("id") or not case.get("role") or not case.get("question"):
            raise ValueError("golden case is missing id, role, or question")
        accepted = case.get("accepted")
        if not isinstance(accepted, list) or not accepted:
            raise ValueError(f"golden case {case['id']} has no accepted results")
        for spec in accepted:
            if not isinstance(spec.get("columns"), list) or not isinstance(spec.get("rows"), list):
                raise ValueError(f"golden case {case['id']} has a bad result")
    return cases


def score_query(sql: str, accepted: list, run) -> bool:
    """Run SQL through the sandbox callable and compare the rows to any accepted result."""
    result = run("read", sql, max_rows=500, dsn=None)
    return matches(result["columns"], result["rows"], accepted)
