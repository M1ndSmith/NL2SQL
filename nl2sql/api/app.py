"""FastAPI process. Handlers stay synchronous because the graph uses invoke."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..agent.graph import build_graph
from ..application import questions
from ..llm.registry import ensure_configured
from ..services import auth, checkpoint, limits, sessions
from ..services.auth import AuthError
from ..services.limits import RateLimitExceeded
from ..application.questions import ThreadForbidden, ThreadNotFound, UnknownProvider
from .routes import auth as auth_routes
from .routes import health, questions as question_routes, reads


def create_app(*, graph=None, authenticator=None, user_loader=None, session_store=None, readiness=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if graph is not None:
            app.state.graph = graph
            yield
            return
        ensure_configured()
        limits.ensure_redis()
        pool = checkpoint.open_pool()
        app.state.pool = pool
        try:
            pool.open()
            app.state.graph = build_graph(checkpoint.prepare(pool))
            yield
        finally:
            pool.close()

    app = FastAPI(lifespan=lifespan)
    app.state.graph = graph
    app.state.authenticator = authenticator or auth.authenticate
    app.state.user_loader = user_loader or auth.get_user
    app.state.sessions = session_store or sessions
    app.state.questions = questions
    app.state.readiness = readiness

    @app.exception_handler(AuthError)
    def authentication_failed(_request, _exc):
        return _error(401, "authentication_failed", "authentication failed")

    @app.exception_handler(RateLimitExceeded)
    def rate_limited(_request, exc: RateLimitExceeded):
        return _error(429, "rate_limited", str(exc))

    @app.exception_handler(ThreadForbidden)
    def thread_forbidden(_request, exc: ThreadForbidden):
        return _error(403, "thread_forbidden", str(exc))

    @app.exception_handler(ThreadNotFound)
    def thread_not_found(_request, exc: ThreadNotFound):
        return _error(404, "thread_not_found", str(exc))

    @app.exception_handler(UnknownProvider)
    def unknown_provider(_request, exc: UnknownProvider):
        return _error(400, "unknown_provider", str(exc))

    @app.exception_handler(RequestValidationError)
    def invalid_body(_request, _exc):
        return _error(422, "invalid_body", "Request body is invalid.")

    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(question_routes.router)
    app.include_router(reads.router)
    return app


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


app = create_app()
