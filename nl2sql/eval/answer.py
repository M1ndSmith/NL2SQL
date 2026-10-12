"""Reject a prose answer that contains a number the result rows do not."""
import re
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)(?![\w.])")


def _decimals(value) -> set[Decimal]:
    found: set[Decimal] = set()
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, (int, float, Decimal)):
        try:
            found.add(Decimal(str(value)))
        except InvalidOperation:
            return found
        return found
    for token in _NUMBER.findall(str(value)):
        try:
            found.add(Decimal(token))
        except InvalidOperation:
            continue
    return found


def grounded(answer: str, rows) -> bool:
    allowed: set[Decimal] = set()
    for row in rows:
        for cell in row:
            allowed |= _decimals(cell)
    for token in _NUMBER.findall(answer or ""):
        try:
            if Decimal(token) not in allowed:
                return False
        except InvalidOperation:
            return False
    return True


def grounded_reply(columns, rows) -> str:
    if not rows:
        return "The query ran but returned no rows."
    header = ", ".join(str(name) for name in columns)
    lines = []
    for row in rows[:20]:
        lines.append(", ".join("" if cell is None else str(cell) for cell in row))
    body = "\n".join(lines)
    return f"Result ({header}):\n{body}" if header else body
