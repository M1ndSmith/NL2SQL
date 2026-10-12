"""Question and approval routes. Both call the application use case."""
from fastapi import APIRouter, Depends, Request

from ..deps import current_principal
from ..schemas import DecisionBody, QuestionBody
from ...application.questions import Turn, UnknownProvider
from ...llm import registry
from ...services.auth import Principal

router = APIRouter()


@router.post("/v1/questions")
def ask(
    body: QuestionBody,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict:
    if body.provider and not registry.enabled(body.provider):
        raise UnknownProvider()
    turn = request.app.state.questions.ask(
        request.app.state.graph,
        principal,
        body.question,
        body.thread_id,
        body.provider,
    )
    return _body(turn)


@router.post("/v1/questions/{thread_id}/decision")
def decide(
    thread_id: str,
    body: DecisionBody,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict:
    turn = request.app.state.questions.decide(
        request.app.state.graph,
        principal,
        thread_id,
        body.action,
        body.sql,
    )
    return _body(turn)


def _body(turn: Turn) -> dict:
    payload = {"status": turn.status, "thread_id": turn.thread_id, "trace": turn.trace}
    if turn.status == "answered":
        payload["answer"] = turn.answer
        if turn.sql is not None:
            payload["sql"] = turn.sql
            payload["rowcount"] = turn.rowcount
            if turn.columns is not None:
                payload["columns"] = turn.columns
                payload["rows"] = turn.rows or []
    else:
        payload["approval"] = turn.approval
    return payload
