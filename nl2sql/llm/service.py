"""LLM calls with per-provider retries, circular fallback, budget, and tracing."""
import asyncio
import logging
from typing import Any

from openai import APIError, APITimeoutError, OpenAIError, RateLimitError
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .. import config
from ..services import limits
from ..services.limits import BudgetExceeded
from . import cost
from . import registry

logger = logging.getLogger(__name__)

_RETRYABLE = (RateLimitError, APITimeoutError, APIError)


class LLMService:
    async def call(
        self,
        messages,
        *,
        response_format: type | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        provider: str | None = None,
    ):
        registry.ensure_configured()
        limits.ensure_redis()
        try:
            return await asyncio.wait_for(
                self._call_with_fallback(messages, response_format, user_id, metadata or {}, provider),
                timeout=config.llm_total_timeout(),
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"llm call timed out after {config.llm_total_timeout()}s total budget"
            ) from exc

    def call_sync(self, messages, **kwargs):
        return asyncio.run(self.call(messages, **kwargs))

    async def _invoke_with_retry(self, runnable, messages, entry: registry.ModelEntry, user_id, metadata):
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(config.max_llm_call_retries()),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type(_RETRYABLE),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                return await self._once(runnable, messages, entry, user_id, metadata)
        raise RuntimeError("llm retry loop exited without a result")

    async def _once(self, runnable, messages, entry: registry.ModelEntry, user_id, metadata):
        input_tokens = cost.count_messages(messages, entry.name)
        reserved = cost.estimate(entry.provider, entry.name, input_tokens, config.max_tokens())
        if reserved > 0:
            limits.reserve(user_id, reserved)
        committed = False
        try:
            response = await self._invoke(runnable, messages, entry, metadata)
            input_tokens, output_tokens = cost.usage(response, messages, entry.name)
            actual = cost.estimate(entry.provider, entry.name, input_tokens, output_tokens)
            if reserved > 0:
                limits.reconcile(user_id, reserved, actual)
            elif actual > 0:
                limits.reserve(user_id, actual)
            committed = True
            self._record_cost(metadata, entry, input_tokens, output_tokens, actual)
            logger.info(
                "llm_call_ok provider=%s model=%s input_tokens=%s output_tokens=%s usd=%.6f",
                entry.provider,
                entry.name,
                input_tokens,
                output_tokens,
                actual,
            )
            return response
        finally:
            if not committed and reserved > 0:
                limits.reconcile(user_id, reserved, 0)

    async def _invoke(self, runnable, messages, entry: registry.ModelEntry, metadata: dict):
        if not config.langfuse_enabled():
            return await runnable.ainvoke(messages)

        from langfuse import get_client, propagate_attributes
        from langfuse.langchain import CallbackHandler

        user = _ascii(metadata.get("user_id") or user_bucket(metadata), 200) or "anonymous"
        thread = _ascii(metadata.get("thread_id"), 200)
        prompt_name = metadata.get("prompt_name") or "llm"
        attributes = {
            "user_id": user,
            "metadata": {
                "role": str(metadata.get("role") or ""),
                "prompt_name": str(prompt_name),
                "prompt_version": str(metadata.get("prompt_version") or ""),
                "provider": entry.provider,
            },
            "trace_name": str(prompt_name),
        }
        if thread:
            attributes["session_id"] = thread
        observation = {
            "as_type": "generation",
            "name": str(prompt_name),
            "model": entry.name,
            "input": messages,
            "metadata": {"provider": entry.provider, "role": metadata.get("role")},
        }
        prompt_client = metadata.get("prompt_client")
        if prompt_client is not None:
            observation["prompt"] = prompt_client
        with propagate_attributes(**attributes):
            with get_client().start_as_current_observation(**observation) as generation:
                response = await runnable.ainvoke(messages, config={"callbacks": [CallbackHandler()]})
                input_tokens, output_tokens = cost.usage(response, messages, entry.name)
                actual = cost.estimate(entry.provider, entry.name, input_tokens, output_tokens)
                generation.update(
                    output=cost.message_text(response)[:4000],
                    usage_details={"input": input_tokens, "output": output_tokens},
                    cost_details={"total": actual},
                )
                return response

    def _record_cost(self, metadata, entry, input_tokens, output_tokens, actual) -> None:
        # Cost is written onto the Langfuse generation inside _invoke when tracing is on.
        metadata["usd"] = actual
        metadata["input_tokens"] = input_tokens
        metadata["output_tokens"] = output_tokens
        metadata["provider"] = entry.provider

    async def _call_with_fallback(self, messages, response_format, user_id, metadata, provider=None):
        models = registry.entries()
        start = registry.provider_index(models, provider) if provider else registry.start_index(models)
        last_error: Exception | None = None
        for offset in range(len(models)):
            entry = models[(start + offset) % len(models)]
            runnable = entry.llm
            if response_format is not None:
                runnable = runnable.with_structured_output(response_format)
            try:
                return await self._invoke_with_retry(runnable, messages, entry, user_id, metadata)
            except BudgetExceeded:
                raise
            except OpenAIError as exc:
                last_error = exc
                logger.error(
                    "llm_provider_failed provider=%s model=%s error=%s",
                    entry.provider,
                    entry.name,
                    exc,
                )
        raise RuntimeError(
            f"failed to get a response after trying {len(models)} providers. last error: {last_error}"
        )


def _ascii(value, limit: int) -> str:
    text = str(value or "")
    cleaned = text.encode("ascii", "ignore").decode("ascii")
    return cleaned[:limit]


def user_bucket(metadata: dict) -> str:
    return metadata.get("user_id") or "anonymous"


def flush_traces() -> None:
    if not config.langfuse_enabled():
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:
        logger.exception("langfuse_flush_failed")


llm_service = LLMService()
