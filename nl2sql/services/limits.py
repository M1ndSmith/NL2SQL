"""Redis budget reservation and per-user rate limits."""
import logging
from datetime import datetime, timedelta, timezone

from .. import config

logger = logging.getLogger(__name__)

_client = None
_redis_ready = False


class BudgetExceeded(RuntimeError):
    pass


class RateLimitExceeded(RuntimeError):
    pass


def set_client(client) -> None:
    global _client, _redis_ready
    _client = client
    _redis_ready = False


def _bucket(user_id: str | None) -> str:
    cleaned = (user_id or "").strip()
    return cleaned or "anonymous"


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def client():
    global _client
    if _client is None:
        url = config.redis_url()
        if not url:
            raise RuntimeError("REDIS_URL is required for budget and rate-limit enforcement.")
        import redis

        _client = redis.Redis.from_url(url, decode_responses=True)
    return _client


def ensure_redis() -> None:
    global _redis_ready
    if _redis_ready:
        return
    client().ping()
    _redis_ready = True


def spent(user_id: str | None, now: datetime | None = None) -> float:
    raw = client().get(budget_key(user_id, now))
    if raw is None:
        return 0.0
    return float(raw)


def budget_key(user_id: str | None, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f"budget:{_bucket(user_id)}:{moment.strftime('%Y-%m-%d')}"


def reserve(user_id: str | None, amount: float) -> None:
    if amount <= 0:
        return
    key = budget_key(user_id)
    store = client()
    total = float(store.incrbyfloat(key, amount))
    store.expire(key, _seconds_until_utc_midnight())
    if total > config.llm_daily_budget_usd() + 1e-9:
        store.incrbyfloat(key, -amount)
        raise BudgetExceeded(
            f"Daily model budget of ${config.llm_daily_budget_usd():.2f} is exhausted for this user."
        )


def reconcile(user_id: str | None, reserved: float, actual: float) -> None:
    delta = actual - reserved
    if abs(delta) < 1e-12:
        return
    store = client()
    key = budget_key(user_id)
    store.incrbyfloat(key, delta)
    store.expire(key, _seconds_until_utc_midnight())


def check_rate(user_id: str | None, now: datetime | None = None) -> None:
    moment = now or datetime.now(timezone.utc)
    key = f"ratelimit:{_bucket(user_id)}:{moment.strftime('%Y%m%d%H%M')}"
    store = client()
    count = int(store.incr(key))
    if count == 1:
        store.expire(key, 60)
    limit = config.rate_limit_per_minute()
    if count > limit:
        raise RateLimitExceeded(f"Rate limit exceeded ({limit} requests per minute).")
