"""OpenAI-compatible providers enabled by whichever credentials are in the environment."""
import logging
from dataclasses import dataclass
from typing import Any

from .. import config

logger = logging.getLogger(__name__)

PROVIDER_ORDER = ("openai", "groq", "nvidia", "ollama")
_ENTRIES: list["ModelEntry"] | None = None
_logged = False


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    name: str
    llm: Any


def ollama_openai_base(url: str | None = None) -> str:
    base = (url if url is not None else config.ollama_base_url()).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def ollama_host(url: str | None = None) -> str:
    base = (url if url is not None else config.ollama_base_url()).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _chat(model: str, api_key: str, base_url: str | None):
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": 0,
        "max_tokens": config.max_tokens(),
    }
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _build() -> list[ModelEntry]:
    found: list[ModelEntry] = []
    if config.openai_api_key():
        found.append(ModelEntry(
            "openai",
            config.openai_model(),
            _chat(config.openai_model(), config.openai_api_key(), config.openai_base_url() or None),
        ))
    if config.groq_api_key():
        found.append(ModelEntry(
            "groq",
            config.groq_model(),
            _chat(config.groq_model(), config.groq_api_key(), "https://api.groq.com/openai/v1"),
        ))
    if config.nvidia_api_key():
        found.append(ModelEntry(
            "nvidia",
            config.nvidia_model(),
            _chat(config.nvidia_model(), config.nvidia_api_key(), config.nvidia_base_url()),
        ))
    if config.ollama_base_url():
        found.append(ModelEntry(
            "ollama",
            config.ollama_model(),
            _chat(config.ollama_model(), config.ollama_api_key(), ollama_openai_base()),
        ))
    return found


def reset() -> None:
    global _ENTRIES, _logged
    _ENTRIES = None
    _logged = False


def entries() -> list[ModelEntry]:
    global _ENTRIES
    if _ENTRIES is None:
        _ENTRIES = _build()
    return _ENTRIES


def provider_index(models: list[ModelEntry], name: str) -> int:
    for index, entry in enumerate(models):
        if entry.provider == name:
            return index
    enabled = ", ".join(entry.provider for entry in models) or "none"
    raise RuntimeError(f"provider {name} is not enabled. Enabled providers: {enabled}.")


def enabled(name: str) -> bool:
    wanted = (name or "").strip().lower()
    return any(entry.provider == wanted for entry in entries())


def catalog() -> list[dict]:
    models = entries()
    if not models:
        return []
    start = start_index(models)
    return [
        {"provider": entry.provider, "model": entry.name, "start": index == start}
        for index, entry in enumerate(models)
    ]


def start_index(models: list[ModelEntry] | None = None) -> int:
    models = entries() if models is None else models
    want = config.llm_provider()
    if not want:
        return 0
    for index, entry in enumerate(models):
        if entry.provider == want:
            return index
    enabled = ", ".join(entry.provider for entry in models) or "none"
    raise RuntimeError(
        f"LLM_PROVIDER={want} is not enabled. Enabled providers: {enabled}."
    )


def ensure_configured() -> list[ModelEntry]:
    global _logged
    models = entries()
    if not models:
        raise RuntimeError(
            "No LLM provider is configured. Set one of OPENAI_API_KEY, GROQ_API_KEY, "
            "NVIDIA_API_KEY, OLLAMA_BASE_URL."
        )
    start = start_index(models)
    if not _logged:
        logger.info(
            "llm_providers_ready start=%s providers=%s",
            models[start].provider,
            [entry.provider for entry in models],
        )
        _logged = True
    return models
