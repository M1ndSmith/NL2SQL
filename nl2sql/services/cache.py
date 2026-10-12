"""Redis LangCache for cheap, already-approved read answers."""
import logging

from langcache.models import SearchStrategy

from .. import config

logger = logging.getLogger(__name__)
_client = None


def _attributes(role: str, user_id: str | None) -> dict[str, str] | None:
    cleaned = (user_id or "").strip()
    if not cleaned:
        return None
    return {"role": role, "user_id": cleaned}


def _service():
    global _client
    if _client is None:
        from langcache import LangCache

        _client = LangCache(
            server_url=config.env("LANGCACHE_SERVER_URL"),
            cache_id=config.env("LANGCACHE_CACHE_ID"),
            api_key=config.env("LANGCACHE_API_KEY"),
        )
    return _client


def search(question: str, role: str, user_id: str | None) -> str | None:
    if not config.langcache_enabled():
        return None
    attributes = _attributes(role, user_id)
    if attributes is None:
        return None
    try:
        found = _service().search(
            prompt=question,
            attributes=attributes,
            similarity_threshold=config.langcache_threshold(),
            search_strategies=[SearchStrategy.EXACT, SearchStrategy.SEMANTIC],
        )
    except Exception:
        logger.exception("langcache_search_failed")
        return None
    if not found.data:
        return None
    return found.data[0].response


def store(question: str, answer: str, role: str, user_id: str | None) -> None:
    if not config.langcache_enabled():
        return
    attributes = _attributes(role, user_id)
    if attributes is None or not answer:
        return
    try:
        _service().set(
            prompt=question,
            response=answer,
            attributes=attributes,
            ttl_millis=config.langcache_ttl_seconds() * 1000,
        )
    except Exception:
        logger.exception("langcache_store_failed")
