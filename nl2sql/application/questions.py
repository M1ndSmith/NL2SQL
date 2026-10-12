"""Ask one question or submit one approval decision."""
import uuid
from dataclasses import dataclass, field

from langgraph.types import Command

from ..llm.service import flush_traces
from ..services import limits, memory
from ..services.auth import Principal


class ThreadForbidden(Exception):
    def __init__(self, message: str = "This thread belongs to another user."):
        super().__init__(message)


class ThreadNotFound(Exception):
    def __init__(self, message: str = "No approval is waiting on this thread."):
        super().__init__(message)


class UnknownProvider(Exception):
    def __init__(self, message: str = "That provider is not enabled."):
        super().__init__(message)


@dataclass(frozen=True)
class Turn:
    thread_id: str
    status: str
    answer: str = ""
    trace: list[str] = field(default_factory=list)
    approval: dict | None = None
    sql: str | None = None
    columns: list | None = None
    rows: list | None = None
    rowcount: int | None = None


def ask(graph, principal: Principal, question: str, thread_id: str | None = None, provider: str | None = None) -> Turn:
    thread_id = (thread_id or "").strip() or str(uuid.uuid4())
    cfg = _cfg(thread_id)
    snapshot = graph.get_state(cfg)
    values = snapshot.values or {}
    if snapshot.interrupts or values.get("user_id"):
        _require_owner(values, principal)
    if snapshot.interrupts:
        return _pending(thread_id, snapshot)
    limits.check_rate(principal.user_id)
    notes = memory.search_sync(principal.user_id, question)
    payload = {
        "question": question,
        "role": principal.role,
        "user_id": principal.user_id,
        "memory": notes,
    }
    if provider:
        payload["provider"] = provider
    result = graph.invoke(payload, cfg)
    return _finish(graph, cfg, thread_id, principal, question, result)


def decide(graph, principal: Principal, thread_id: str, action: str, sql: str | None = None) -> Turn:
    cfg = _cfg(thread_id)
    snapshot = graph.get_state(cfg)
    values = snapshot.values or {}
    if not snapshot.interrupts:
        raise ThreadNotFound()
    _require_owner(values, principal)
    resume = {"action": action}
    if action == "edit":
        resume["sql"] = sql or ""
    result = graph.invoke(Command(resume=resume), cfg)
    return _finish(graph, cfg, thread_id, principal, values.get("question") or "", result)


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _require_owner(values: dict, principal: Principal) -> None:
    if values.get("user_id") != principal.user_id or values.get("role") != principal.role:
        raise ThreadForbidden()


def _pending(thread_id: str, snapshot) -> Turn:
    return Turn(
        thread_id=thread_id,
        status="awaiting_approval",
        trace=list((snapshot.values or {}).get("trace") or []),
        approval=_payload(snapshot.interrupts),
    )


def _finish(graph, cfg: dict, thread_id: str, principal: Principal, question: str, result: dict) -> Turn:
    if result.get("__interrupt__"):
        flush_traces()
        snapshot = graph.get_state(cfg)
        return Turn(
            thread_id=thread_id,
            status="awaiting_approval",
            trace=list((snapshot.values or {}).get("trace") or []),
            approval=_payload(result["__interrupt__"]),
        )
    answer = result.get("answer") or ""
    if result.get("remember"):
        memory.add_sync(principal.user_id, question, answer)
    flush_traces()
    return Turn(
        thread_id=thread_id,
        status="answered",
        answer=answer,
        trace=list(result.get("trace") or []),
        **_executed(result),
    )


def _executed(result: dict) -> dict:
    ran = False
    for item in result.get("trace") or []:
        text = str(item)
        if text == "executed write" or text.startswith("executed read"):
            ran = True
            break
    if not ran:
        return {}
    columns = result.get("columns")
    rows = result.get("rows")
    rowcount = result.get("rowcount")
    if rowcount is None and rows is not None:
        rowcount = len(rows)
    fields = {"sql": result.get("sql") or "", "rowcount": 0 if rowcount is None else rowcount}
    if columns is not None:
        fields["columns"] = columns
        fields["rows"] = rows or []
    return fields


def _payload(interrupts) -> dict:
    first = interrupts[0]
    value = first.value if hasattr(first, "value") else first
    return dict(value)
