"""python run.py <user_id> <question> [thread_id]

Local client of the question use case. The deployed process is the API.
"""
import getpass
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nl2sql import config  # noqa: E402
from nl2sql.agent.graph import build_graph  # noqa: E402
from nl2sql.application import questions  # noqa: E402
from nl2sql.application.questions import ThreadForbidden, ThreadNotFound  # noqa: E402
from nl2sql.llm.registry import ensure_configured  # noqa: E402
from nl2sql.services import checkpoint, limits  # noqa: E402
from nl2sql.services.auth import AuthError, authenticate  # noqa: E402
from nl2sql.services.limits import RateLimitExceeded  # noqa: E402


def main(user_id: str, question: str, thread_id: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    password = _password()
    try:
        principal = authenticate(user_id, password)
    except AuthError as exc:
        print(str(exc))
        raise SystemExit(1) from exc
    if not config.checkpoint_dsn():
        raise RuntimeError("NL2SQL_CHECKPOINT_DSN is required for the Postgres checkpointer.")
    ensure_configured()
    limits.ensure_redis()
    thread_id = (thread_id or config.env("NL2SQL_THREAD_ID") or "").strip() or None

    with checkpoint.open_pool() as pool:
        graph = build_graph(checkpoint.prepare(pool))
        try:
            turn = questions.ask(graph, principal, question, thread_id)
            print(f"thread_id: {turn.thread_id}")
            while turn.status == "awaiting_approval":
                print("\n--- APPROVAL REQUIRED ---")
                for key, value in (turn.approval or {}).items():
                    print(f"{key}: {value}")
                action = input("\naction [approve/reject/edit]: ").strip().lower()
                sql = input("new sql: ").strip() if action == "edit" else None
                turn = questions.decide(graph, principal, turn.thread_id, action, sql)
        except (RateLimitExceeded, ThreadForbidden, ThreadNotFound) as exc:
            print(str(exc))
            raise SystemExit(1) from exc
        print("\n".join(f"  · {line}" for line in turn.trace))
        print("\n" + turn.answer)


def _password() -> str:
    password = config.env("NL2SQL_PASSWORD")
    if password:
        return password
    if sys.stdin.isatty():
        return getpass.getpass("password: ")
    raise SystemExit("NL2SQL_PASSWORD is required when stdin is not a TTY.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: python run.py <user_id> <question> [thread_id]")
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
