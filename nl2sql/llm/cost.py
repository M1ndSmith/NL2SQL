"""Token counts and USD estimates for model calls."""
import tiktoken

from .. import config


def _encoding(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


def count_text(text: str, model: str) -> int:
    if not text:
        return 0
    try:
        return len(_encoding(model).encode(text))
    except Exception:
        # tiktoken fetches its blob on first use. A rough 4-chars-per-token estimate
        # keeps the budget check working when that fetch is unavailable.
        return max(1, len(text) // 4)


def message_text(message) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(getattr(block, "text", "")))
        return "".join(parts)
    if hasattr(message, "model_dump_json"):
        return message.model_dump_json()
    return str(content)


def prompt_text(messages) -> str:
    chunks: list[str] = []
    for message in messages:
        if isinstance(message, tuple) and len(message) == 2:
            chunks.append(str(message[1]))
        elif isinstance(message, dict):
            chunks.append(str(message.get("content", "")))
        else:
            chunks.append(message_text(message))
    return "\n".join(chunks)


def count_messages(messages, model: str) -> int:
    return count_text(prompt_text(messages), model)


def estimate(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    table = config.prices()
    if provider == "ollama" and model not in table:
        return 0.0
    input_price, output_price = table.get(model, config.UNKNOWN_PRICE)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def usage(response, messages, model: str) -> tuple[int, int]:
    meta = getattr(response, "usage_metadata", None) or {}
    if meta.get("input_tokens") is not None:
        return int(meta["input_tokens"]), int(meta.get("output_tokens") or 0)
    return count_messages(messages, model), count_text(message_text(response), model)
