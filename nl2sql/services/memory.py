"""Long-term memory via mem0. The LangGraph checkpointer stays short-term."""
import asyncio
import logging

from .. import config
from ..llm import registry

logger = logging.getLogger(__name__)
_memory = None
_embedder_warned = False

_EMBED_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "nomic-embed-text": 768,
    "nvidia/nv-embedqa-e5-v5": 1024,
}


def _dims(model: str) -> int:
    override = config.embed_dims()
    if override:
        return override
    return _EMBED_DIMS.get(model, 1536)


def _drop_empty(values: dict) -> dict:
    return {key: value for key, value in values.items() if value not in (None, "")}


def embedder_config() -> dict | None:
    if config.openai_api_key():
        model = config.openai_embed_model()
        return {
            "provider": "openai",
            "config": _drop_empty({
                "model": model,
                "api_key": config.openai_api_key(),
                "openai_base_url": config.openai_base_url(),
                "embedding_dims": _dims(model),
            }),
        }
    if config.ollama_base_url():
        model = config.ollama_embed_model()
        return {
            "provider": "ollama",
            "config": {
                "model": model,
                "ollama_base_url": registry.ollama_host(),
                "embedding_dims": _dims(model),
            },
        }
    if config.nvidia_api_key():
        model = config.nvidia_embed_model()
        return {
            "provider": "openai",
            "config": {
                "model": model,
                "api_key": config.nvidia_api_key(),
                "openai_base_url": config.nvidia_base_url(),
                "embedding_dims": _dims(model),
            },
        }
    return None


def _warn_missing_embedder() -> None:
    global _embedder_warned
    if _embedder_warned:
        return
    _embedder_warned = True
    logger.warning(
        "long_term_memory_disabled: no embedder credential "
        "(OPENAI_API_KEY, OLLAMA_BASE_URL, or NVIDIA_API_KEY)"
    )


def _llm_config() -> dict:
    models = registry.entries()
    entry = models[registry.start_index(models)]
    if entry.provider == "groq":
        return {"provider": "groq", "config": {"model": entry.name, "api_key": config.groq_api_key(), "temperature": 0}}
    if entry.provider == "ollama":
        return {
            "provider": "ollama",
            "config": {"model": entry.name, "ollama_base_url": registry.ollama_host(), "temperature": 0},
        }
    if entry.provider == "nvidia":
        return {
            "provider": "openai",
            "config": {
                "model": entry.name,
                "api_key": config.nvidia_api_key(),
                "openai_base_url": config.nvidia_base_url(),
                "temperature": 0,
            },
        }
    return _drop_empty({
        "provider": "openai",
        "config": _drop_empty({
            "model": entry.name,
            "api_key": config.openai_api_key(),
            "openai_base_url": config.openai_base_url(),
            "temperature": 0,
        }),
    })


def _enabled(user_id: str | None) -> bool:
    if not (user_id or "").strip() or not config.memory_dsn():
        return False
    if embedder_config() is None:
        _warn_missing_embedder()
        return False
    return True


async def _client():
    global _memory
    if _memory is None:
        from mem0 import AsyncMemory

        embedder = embedder_config()
        _memory = AsyncMemory.from_config({
            "vector_store": {
                "provider": "pgvector",
                "config": {
                    "connection_string": config.memory_dsn(),
                    "collection_name": config.memory_collection(),
                    "embedding_model_dims": _dims(embedder["config"].get("model", "")),
                },
            },
            "llm": _llm_config(),
            "embedder": embedder,
        })
    return _memory


def _rows(results) -> list:
    if isinstance(results, dict):
        return list(results.get("results") or [])
    return list(results or [])


async def _search(user_id: str, query: str) -> str:
    memory = await _client()
    results = await memory.search(query, filters={"user_id": user_id}, top_k=5)
    lines = []
    for row in _rows(results):
        text = row.get("memory") if isinstance(row, dict) else str(row)
        if text:
            lines.append(f"* {text}")
    return "\n".join(lines)


async def _add(user_id: str, question: str, answer: str) -> None:
    memory = await _client()
    await memory.add(
        [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ],
        user_id=user_id,
    )


def search_sync(user_id: str | None, query: str) -> str:
    if not _enabled(user_id):
        return ""
    try:
        return asyncio.run(_search(user_id.strip(), query))
    except Exception:
        logger.exception("memory_search_failed")
        return ""


def add_sync(user_id: str | None, question: str, answer: str) -> None:
    if not _enabled(user_id) or not answer:
        return
    try:
        asyncio.run(_add(user_id.strip(), question, answer))
        logger.info("long_term_memory_updated user_id=%s", user_id)
    except Exception:
        logger.exception("memory_add_failed")
