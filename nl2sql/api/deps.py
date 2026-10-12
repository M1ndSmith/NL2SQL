"""Bearer session -> principal. The role is loaded from the auth store, not the token."""
from typing import Annotated

from fastapi import Header, Request

from ..services.auth import AuthError, Principal


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization:
        raise AuthError()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError()
    return token.strip()


def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    token = bearer_token(authorization)
    user_id = request.app.state.sessions.read_session(token)
    if not user_id:
        raise AuthError()
    return request.app.state.user_loader(user_id)
