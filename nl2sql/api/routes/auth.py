"""Login checks the password once. Later requests send the opaque session token."""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response

from ..deps import bearer_token, current_principal
from ..schemas import LoginBody
from ...services.auth import AuthError, Principal

router = APIRouter()


@router.post("/v1/auth/login")
def login(body: LoginBody, request: Request) -> dict:
    principal = request.app.state.authenticator(body.user_id, body.password)
    token, expires_in = request.app.state.sessions.create_session(principal.user_id)
    return {"token": token, "expires_in": expires_in}


@router.get("/v1/me")
def me(principal: Principal = Depends(current_principal)) -> dict:
    return {"user_id": principal.user_id, "role": principal.role}


@router.post("/v1/auth/logout", status_code=204)
def logout(request: Request, authorization: Annotated[str | None, Header()] = None) -> Response:
    token = bearer_token(authorization)
    if not request.app.state.sessions.read_session(token):
        raise AuthError()
    request.app.state.sessions.delete_session(token)
    return Response(status_code=204)
