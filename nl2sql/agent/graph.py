"""NL -> SQL LangGraph agent with a production-style harness.

START -> route -> [cache] -> load_schema -> generate -> guard -> preflight -> [approval] -> execute -> respond
                                            ^___________|__________|__________________|
                                                  bounded repair loop (MAX_ATTEMPTS)
"""
import logging
import operator
from dataclasses import replace
from typing import Annotated, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from .. import config
from ..eval.accuracy import same_rows
from ..eval.answer import grounded, grounded_reply
from ..llm import prompts
from ..llm import service as llm
from ..llm.cost import message_text
from ..services import audit
from ..services import cache as answer_cache
from ..services.limits import BudgetExceeded
from . import evidence, sandbox
from .guardrails import GuardrailError, check
from .redact import redact
from .sandbox import SandboxError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- state
class State(TypedDict, total=False):
    question: str
    role: str                      # injected by your auth layer
    user_id: str
    provider: str                  # optional start of the provider circle for this question
    memory: str                    # untrusted long-term notes
    intent: str
    schema: str
    sql: str
    alternate: str                 # second read candidate; empty for a write
    kind: str                      # read | write
    cost: float
    preview: dict
    attempts: int
    error: str
    columns: list
    rows: list
    rowcount: int
    answer: str
    remember: bool
    trace: Annotated[list[str], operator.add]


class Route(BaseModel):
    intent: Literal["query", "write", "clarify", "refuse"]
    reason: str = Field(description="One short sentence.")
    clarifying_question: str = ""


class Draft(BaseModel):
    sql: str = Field(description="A single PostgreSQL statement, no comments, no trailing semicolon needed.")
    alternate: str = Field(default="", description="A second SELECT for the same question, or empty for a write.")


def _policy(state: State) -> config.Policy:
    return config.ROLES[state["role"]]  # unknown role -> KeyError -> fail closed


def _guard_policy(state: State) -> config.Policy:
    """A role with no table list may use only the tables its login just exposed."""
    pol = _policy(state)
    if pol.allowed_tables:
        return pol
    cached = _schema_cache.get(pol.role)
    visible = cached.visible if cached else frozenset()
    return replace(pol, allowed_tables=visible)


def _notes(state: State) -> str:
    return (state.get("memory") or "").strip() or "(none)"


def _meta(state: State, config: RunnableConfig, prompt: prompts.LoadedPrompt) -> dict:
    configurable = (config or {}).get("configurable") or {}
    return {
        "user_id": state.get("user_id") or "",
        "thread_id": str(configurable.get("thread_id") or ""),
        "role": state.get("role") or "",
        "prompt_name": prompt.name,
        "prompt_version": prompt.version,
        "prompt_client": prompt.client,
    }


def _model(state, config, messages, response_format, prompt: prompts.LoadedPrompt, *, failed_goto: str):
    try:
        return llm.llm_service.call_sync(
            messages,
            response_format=response_format,
            user_id=state.get("user_id") or None,
            metadata=_meta(state, config, prompt),
            provider=(state.get("provider") or "").strip() or None,
        ), None
    except BudgetExceeded as exc:
        return None, Command(update={"answer": str(exc), "trace": ["budget exceeded"]}, goto=END)
    except Exception as exc:
        logger.exception("llm_call_failed")
        update = {"error": str(exc), "trace": [f"llm failed: {str(exc)[:160]}"]}
        if failed_goto == END:
            update["answer"] = f"The model call failed: {exc}"
        return None, Command(update=update, goto=failed_goto)


# ---------------------------------------------------------------- nodes
def route_node(state: State, config: RunnableConfig) -> Command:
    question = redact(state["question"])
    prompt = prompts.load("nl2sql-route", prompts.ROUTE, {"memory": _notes(state), "question": question})
    result, failed = _model(
        state,
        config,
        [("system", prompt.text), ("human", question)],
        Route,
        prompt,
        failed_goto=END,
    )
    if failed:
        return failed
    decision: Route = result
    note = f"route:{decision.intent}"
    if decision.intent == "write" and not _policy(state).can_write:
        return Command(update={"intent": "refuse", "answer": "Your role is not permitted to modify data.",
                               "trace": [note + " (denied by role)"]}, goto=END)
    if decision.intent == "refuse":
        return Command(update={"intent": decision.intent, "answer": f"I can't help with that: {decision.reason}",
                               "trace": [note]}, goto=END)
    if decision.intent == "clarify":
        return Command(update={"intent": decision.intent,
                               "answer": decision.clarifying_question or "Could you be more specific?",
                               "trace": [note]}, goto=END)
    nxt = "cache_lookup" if decision.intent == "query" else "load_schema"
    return Command(update={"intent": decision.intent, "attempts": 0, "trace": [note]}, goto=nxt)


def cache_lookup_node(state: State) -> Command:
    if state.get("intent") != "query":
        return Command(goto="load_schema")
    if not config.langcache_enabled():
        return Command(update={"trace": ["cache off"]}, goto="load_schema")
    hit = answer_cache.search(state["question"], state["role"], state.get("user_id"))
    if hit:
        return Command(update={"answer": hit, "remember": True, "trace": ["cache hit"]}, goto=END)
    return Command(update={"trace": ["cache miss"]}, goto="load_schema")


_schema_cache: dict[str, evidence.Catalog] = {}


def load_schema_node(state: State) -> dict:
    pol = _policy(state)
    catalog = _schema_cache.get(pol.role)
    if catalog is None:
        catalog = evidence.load_catalog(sandbox.run, pol)
        _schema_cache[pol.role] = catalog
    text = evidence.prompt_for(sandbox.run, pol, catalog, state.get("question") or "")
    return {"schema": text}


def _retry(state: State, msg: str, stage: str) -> Command:
    n = state.get("attempts", 0) + 1
    upd = {"attempts": n, "error": msg, "trace": [f"{stage} failed ({n}/{config.MAX_ATTEMPTS}): {msg[:120]}"]}
    return Command(update=upd, goto="give_up" if n >= config.MAX_ATTEMPTS else "generate")


def generate_node(state: State, config: RunnableConfig) -> Command:
    pol = _policy(state)
    write_rule = (
        "this is a WRITE request: include a precise WHERE clause."
        if state["intent"] == "write"
        else "read-only SELECT."
    )
    repair = ""
    if state.get("error"):
        repair = f"\n\nYour previous SQL:\n{state.get('sql', '')}\nFailed with:\n{state['error']}\nFix it."
    question = redact(state["question"])
    prompt = prompts.load("nl2sql-generate", prompts.GENERATE, {
        "schema": state.get("schema") or "",
        "privilege": prompts.privilege_line(pol),
        "write_rule": write_rule,
        "memory": _notes(state),
        "question": question,
        "repair": repair,
    })
    result, failed = _model(
        state,
        config,
        [("system", prompt.text), ("human", question + repair)],
        Draft,
        prompt,
        failed_goto="give_up",
    )
    if failed:
        return failed
    draft: Draft = result
    alternate = (draft.alternate or "").strip().rstrip(";")
    return Command(
        update={"sql": draft.sql.strip().rstrip(";"), "alternate": alternate, "trace": ["generate"]},
        goto="guard",
    )


def guard_node(state: State) -> Command:
    try:
        checked = check(state["sql"], _guard_policy(state))
    except GuardrailError as exc:
        return _retry(state, f"guardrail: {exc}", "guard")
    return Command(update={"kind": checked.kind, "sql": checked.sql, "trace": [f"guard ok ({checked.kind})"]},
                   goto="preflight")


def preflight_node(state: State) -> Command:
    """Side-effect-free probe (EXPLAIN cost / rolled-back dry run). Kept separate from the
    approval node because interrupt() re-runs its node from the top on resume."""
    pol = _policy(state)
    dsn = config.role_dsn(pol.role)
    try:
        if state["kind"] == "write":
            affected = sandbox.run("dry_write", state["sql"], dsn=dsn)["rowcount"]
            preview = {"dry_run_rows_affected": affected, "exceeds_write_threshold": affected > pol.max_write_rows}
            return Command(update={"preview": preview, "trace": [f"dry-run: {affected} rows"]}, goto="approval")
        planner_cost = sandbox.run("explain", state["sql"], dsn=dsn)["cost"]
    except SandboxError as exc:
        if exc.retryable:
            return _retry(state, str(exc), "preflight")
        return Command(update={"error": str(exc)}, goto="give_up")
    return Command(
        update={"cost": planner_cost, "preview": {"estimated_cost": planner_cost},
                "trace": [f"explain cost={planner_cost:.0f}"]},
        goto="agree",
    )


def _after_agree(state: State) -> str:
    if (state.get("cost") or 0) > config.COST_APPROVAL_THRESHOLD:
        return "approval"
    return "execute"


def agree_node(state: State) -> Command:
    """Run a second read-only candidate. Different result sets are not answered."""
    alternate = (state.get("alternate") or "").strip()
    if state.get("kind") != "read" or not alternate:
        return Command(goto=_after_agree(state))
    pol = _policy(state)
    dsn = config.role_dsn(pol.role)
    try:
        checked = check(alternate, _guard_policy(state))
        primary = sandbox.run("read", state["sql"], max_rows=pol.max_rows, dsn=dsn)
        second = sandbox.run("read", checked.sql, max_rows=pol.max_rows, dsn=dsn)
    except GuardrailError as exc:
        return _retry(state, f"guardrail: {exc}", "agree")
    except SandboxError as exc:
        if exc.retryable:
            return _retry(state, str(exc), "agree")
        return Command(update={"error": str(exc)}, goto="give_up")
    if not same_rows(primary["columns"], primary["rows"], second["columns"], second["rows"]):
        return _retry(state, "two SQL candidates returned different rows", "agree")
    return Command(update={"trace": ["candidates agree"]}, goto=_after_agree(state))


def approval_node(state: State) -> Command:
    """First HITL interrupt: all writes, and reads the planner thinks are expensive."""
    decision = interrupt({
        "type": "sql_approval",
        "question": state["question"],
        "kind": state["kind"],
        "sql": state["sql"],
        **state.get("preview", {}),
        "options": ["approve", "reject", "edit (provide 'sql')"],
    })
    action = (decision or {}).get("action", "reject")
    if action == "approve":
        return Command(update={"trace": ["human: approved"]}, goto="execute")
    if action == "edit" and decision.get("sql"):
        # Edited SQL is untrusted: it goes back through guardrails + preflight + approval.
        return Command(update={"sql": decision["sql"], "trace": ["human: edited"]}, goto="guard")
    return Command(update={"answer": "Cancelled by reviewer. Nothing was executed.", "trace": ["human: rejected"]},
                   goto=END)


def execute_node(state: State) -> Command:
    pol = _policy(state)
    dsn = config.role_dsn(pol.role)
    try:
        if state["kind"] == "write":
            res = sandbox.run("write", state["sql"], dsn=dsn)
            audit.record(
                state.get("user_id") or "",
                pol.role,
                state.get("sql") or "",
                res["rowcount"],
                _decision(state),
            )
            return Command(update={"rowcount": res["rowcount"], "trace": ["executed write"]}, goto="respond")
        res = sandbox.run("read", state["sql"], max_rows=pol.max_rows, dsn=dsn)
    except SandboxError as exc:
        if exc.retryable:
            return _retry(state, str(exc), "execute")
        return Command(update={"error": str(exc)}, goto="give_up")
    audit.record(
        state.get("user_id") or "",
        pol.role,
        state.get("sql") or "",
        len(res["rows"]),
        _decision(state),
    )
    return Command(update={"columns": res["columns"], "rows": res["rows"],
                           "trace": [f"executed read: {len(res['rows'])} rows"]}, goto="respond")


def _decision(state: State) -> str:
    if any("human: approved" in item for item in state.get("trace") or []):
        return "approved"
    return "executed"


def _maybe_cache(state: State, answer: str) -> list[str]:
    if state.get("kind") != "read" or not config.langcache_enabled():
        return []
    if "cost" not in state or state["cost"] > config.COST_APPROVAL_THRESHOLD:
        return []
    answer_cache.store(state["question"], answer, state["role"], state.get("user_id"))
    return ["cached answer"]


def respond_node(state: State, config: RunnableConfig) -> dict:
    if state["kind"] == "write":
        answer = f"Done. {state['rowcount']} row(s) affected."
        return {"answer": answer, "remember": True}
    if not state["rows"]:
        answer = "The query ran but returned no rows."
        return {"answer": answer, "remember": True, "trace": _maybe_cache(state, answer)}
    sample = state["rows"][:20]
    question = redact(state["question"])
    prompt = prompts.load("nl2sql-respond", prompts.RESPOND, {
        "question": question,
        "sql": state.get("sql") or "",
        "columns": state["columns"],
        "rows": sample,
        "row_count": len(state["rows"]),
        "sample_count": len(sample),
    })
    human = (
        f"Question: {question}\nSQL: {state['sql']}\nColumns: {state['columns']}\n"
        f"Rows (first {len(sample)} of {len(state['rows'])}): {sample}"
    )
    result, failed = _model(
        state,
        config,
        [("system", prompt.text), ("human", human)],
        None,
        prompt,
        failed_goto="give_up",
    )
    if failed:
        return failed
    answer = message_text(result)
    trace = ["respond"]
    if not grounded(answer, sample):
        answer = grounded_reply(state["columns"], sample)
        trace.append("answer rejected invented number")
    return {"answer": answer, "remember": True, "trace": [*trace, *_maybe_cache(state, answer)]}


def give_up_node(state: State) -> dict:
    return {"answer": f"I couldn't produce a safe, working query ({state.get('error', 'unknown error')}). "
                      "Try rephrasing or narrowing the question."}


# ---------------------------------------------------------------- assembly
def build_graph(checkpointer):
    if checkpointer is None:
        raise RuntimeError(
            "A checkpointer is required. Production passes PostgresSaver; tests pass InMemorySaver."
        )
    graph = StateGraph(State)
    for name, fn in [
        ("route", route_node), ("cache_lookup", cache_lookup_node), ("load_schema", load_schema_node),
        ("generate", generate_node), ("guard", guard_node), ("preflight", preflight_node),
        ("agree", agree_node), ("approval", approval_node), ("execute", execute_node),
        ("respond", respond_node), ("give_up", give_up_node),
    ]:
        graph.add_node(name, fn)
    graph.add_edge(START, "route")
    graph.add_edge("load_schema", "generate")
    graph.add_edge("respond", END)
    graph.add_edge("give_up", END)
    return graph.compile(checkpointer=checkpointer)
