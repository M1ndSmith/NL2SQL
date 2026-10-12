"""Central config: role policies, limits, DSNs, and provider credentials."""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
_ROLE_KEYS = ("can_write", "max_rows", "max_write_rows")


def _load_dotenv() -> None:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env(name)
    return int(raw) if raw else default


def env_float(name: str, default: float) -> float:
    raw = env(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Policy:
    role: str
    allowed_tables: frozenset[str]
    denied_columns: frozenset[str] = frozenset()  # "table.column"
    can_write: bool = False
    max_rows: int = 500
    max_write_rows: int = 100  # writes touching more rows get flagged in the approval payload


def _mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"config.yaml {label} must be a mapping")
    return value


def load_file(path: Path) -> dict:
    """Read the YAML config. A missing file is a startup failure."""
    if not path.is_file():
        raise RuntimeError(f"Missing config file: {path}")
    return _mapping(yaml.safe_load(path.read_text()), "root")


def _names(values, pattern: re.Pattern[str], label: str) -> frozenset[str]:
    if not values:
        return frozenset()
    if not isinstance(values, list):
        raise RuntimeError(f"config.yaml {label} must be a list")
    names = []
    for item in values:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise RuntimeError(f"Invalid {label}: {item}")
        names.append(item)
    return frozenset(names)


def _roles(raw) -> dict[str, Policy]:
    body = _mapping(raw, "roles")
    if not body:
        raise RuntimeError("config.yaml roles is empty")
    roles: dict[str, Policy] = {}
    for name, spec in body.items():
        if not isinstance(name, str) or not _IDENT.fullmatch(name):
            raise RuntimeError(f"Invalid role name: {name}")
        spec = _mapping(spec, f"role {name}")
        missing = [key for key in _ROLE_KEYS if key not in spec]
        if missing:
            raise RuntimeError(f"config.yaml role '{name}' missing {missing[0]}")
        roles[name] = Policy(
            name,
            _names(spec.get("tables"), _IDENT, "table name"),
            denied_columns=_names(spec.get("denied_columns"), _COLUMN, "denied column"),
            can_write=bool(spec["can_write"]),
            max_rows=int(spec["max_rows"]),
            max_write_rows=int(spec["max_write_rows"]),
        )
    return roles


def _price_table(raw) -> dict[str, tuple[float, float]]:
    body = _mapping(raw, "llm.prices")
    table: dict[str, tuple[float, float]] = {}
    for model, pair in body.items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise RuntimeError(f"config.yaml price for {model} must be [input, output]")
        table[str(model)] = (float(pair[0]), float(pair[1]))
    return table


def schema_name_filter(tables: frozenset[str]) -> str:
    """Extra schema predicate. Empty means every public table the login can see."""
    if not tables:
        return ""
    quoted = []
    for name in sorted(tables):
        if not _IDENT.fullmatch(name):
            raise RuntimeError(f"Invalid table name: {name}")
        quoted.append(f"'{name}'")
    return " AND table_name IN (" + ", ".join(quoted) + ")"


_FILE = load_file(Path(__file__).resolve().parents[1] / "config.yaml")
_LLM = _mapping(_FILE.get("llm"), "llm")
_LIMITS = _mapping(_FILE.get("limits"), "limits")
_LANGCACHE = _mapping(_FILE.get("langcache"), "langcache")
_LANGFUSE = _mapping(_FILE.get("langfuse"), "langfuse")
_MEMORY = _mapping(_FILE.get("memory"), "memory")
_MODELS = _mapping(_LLM.get("models"), "llm.models")
_EMBED_MODELS = _mapping(_LLM.get("embed_models"), "llm.embed_models")

# Role comes from the auth row, never from the model or the user's text.
# An empty allowlist means the schema is whatever that login can see.
ROLES: dict[str, Policy] = _roles(_FILE.get("roles"))

_ROLE_DSN_DEFAULTS = {
    "analyst": "postgresql://nl2sql_analyst:pw@localhost:5432/app",
    "ops": "postgresql://nl2sql_ops:pw@localhost:5432/app",
}


def _setting(section: dict, key: str):
    if key not in section:
        raise RuntimeError(f"config.yaml missing '{key}'")
    return section[key]


def auth_dsn() -> str:
    return env("NL2SQL_AUTH_DSN")


def audit_dsn() -> str:
    return env("NL2SQL_AUDIT_DSN")


def distinct_value_cap() -> int:
    return int(_setting(_LIMITS, "distinct_value_cap"))


def schema_table_cap() -> int:
    """0 means send every visible table. A positive cap ranks by the question, then keeps that many."""
    return int(_setting(_LIMITS, "schema_table_cap"))


def session_ttl_seconds() -> int:
    return env_int("NL2SQL_SESSION_TTL_SECONDS", int(_setting(_LIMITS, "session_ttl_seconds")))


def role_dsn(role: str) -> str:
    """Database login for this role. The sandbox has no shared writer fallback."""
    if role not in ROLES:
        raise KeyError(role)
    default = _ROLE_DSN_DEFAULTS.get(role, "")
    found = env(f"NL2SQL_{role.upper()}_DSN", default) or default
    if not found:
        raise KeyError(role)
    return found


def _limit_int(key: str) -> int:
    return int(_setting(_LIMITS, key))


# Harness limits. The environment does not replace these; edit config.yaml.
MAX_ATTEMPTS = _limit_int("max_attempts")
COST_APPROVAL_THRESHOLD = float(_setting(_LIMITS, "cost_approval_threshold"))
STATEMENT_TIMEOUT_MS = _limit_int("statement_timeout_ms")
WALL_CLOCK_S = _limit_int("wall_clock_s")
MEM_BYTES = _limit_int("sandbox_mem_bytes")

# Prices are USD per 1M tokens. Unknown cloud models use a high default so they cannot slip the budget.
UNKNOWN_PRICE = (30.0, 60.0)
DEFAULT_PRICES: dict[str, tuple[float, float]] = _price_table(_LLM.get("prices"))


def prices() -> dict[str, tuple[float, float]]:
    table = dict(DEFAULT_PRICES)
    raw = env("LLM_PRICES_JSON")
    if not raw:
        return table
    parsed = json.loads(raw)
    for model, pair in parsed.items():
        table[str(model)] = (float(pair[0]), float(pair[1]))
    return table


def max_tokens() -> int:
    return env_int("MAX_TOKENS", int(_setting(_LLM, "max_tokens")))


def max_llm_call_retries() -> int:
    return env_int("MAX_LLM_CALL_RETRIES", int(_setting(_LLM, "max_retries")))


def llm_total_timeout() -> int:
    return env_int("LLM_TOTAL_TIMEOUT", int(_setting(_LLM, "total_timeout")))


def llm_daily_budget_usd() -> float:
    return env_float("LLM_DAILY_BUDGET_USD", float(_setting(_LLM, "daily_budget_usd")))


def rate_limit_per_minute() -> int:
    return env_int("RATE_LIMIT_PER_MINUTE", int(_setting(_LIMITS, "rate_per_minute")))


def redis_url() -> str:
    return env("REDIS_URL")


def llm_provider() -> str:
    chosen = env("LLM_PROVIDER") or str(_LLM.get("provider") or "")
    return chosen.lower()


def openai_api_key() -> str:
    return env("OPENAI_API_KEY")


def openai_base_url() -> str:
    return env("OPENAI_BASE_URL")


def openai_model() -> str:
    return env("OPENAI_MODEL") or str(_setting(_MODELS, "openai"))


def groq_api_key() -> str:
    return env("GROQ_API_KEY")


def groq_model() -> str:
    return env("GROQ_MODEL") or str(_setting(_MODELS, "groq"))


def nvidia_api_key() -> str:
    return env("NVIDIA_API_KEY")


def nvidia_model() -> str:
    return env("NVIDIA_MODEL") or str(_setting(_MODELS, "nvidia"))


def nvidia_base_url() -> str:
    return env("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")


def ollama_base_url() -> str:
    return env("OLLAMA_BASE_URL")


def ollama_api_key() -> str:
    return env("OLLAMA_API_KEY", "ollama")


def ollama_model() -> str:
    return env("OLLAMA_MODEL") or str(_setting(_MODELS, "ollama"))


def openai_embed_model() -> str:
    return env("OPENAI_EMBED_MODEL") or str(_setting(_EMBED_MODELS, "openai"))


def ollama_embed_model() -> str:
    return env("OLLAMA_EMBED_MODEL") or str(_setting(_EMBED_MODELS, "ollama"))


def nvidia_embed_model() -> str:
    return env("NVIDIA_EMBED_MODEL") or str(_setting(_EMBED_MODELS, "nvidia"))


def memory_dsn() -> str:
    return env("NL2SQL_MEMORY_DSN")


def checkpoint_dsn() -> str:
    return env("NL2SQL_CHECKPOINT_DSN")


def memory_collection() -> str:
    return env("NL2SQL_MEMORY_COLLECTION") or str(_setting(_MEMORY, "collection"))


def embed_dims() -> int | None:
    raw = env("NL2SQL_EMBED_DIMS")
    if raw:
        return int(raw)
    value = _MEMORY.get("embed_dims")
    if value in (None, ""):
        return None
    return int(value)


def langfuse_enabled() -> bool:
    raw = env("LANGFUSE_TRACING_ENABLED")
    if raw:
        enabled = raw.lower() not in {"false", "0", "no", "off"}
    else:
        enabled = bool(_setting(_LANGFUSE, "tracing_enabled"))
    if not enabled:
        return False
    return bool(env("LANGFUSE_PUBLIC_KEY") and env("LANGFUSE_SECRET_KEY"))


def langcache_enabled() -> bool:
    return bool(env("LANGCACHE_SERVER_URL") and env("LANGCACHE_API_KEY") and env("LANGCACHE_CACHE_ID"))


def langcache_threshold() -> float:
    return env_float("LANGCACHE_SIMILARITY_THRESHOLD", float(_setting(_LANGCACHE, "similarity_threshold")))


def langcache_ttl_seconds() -> int:
    return env_int("LANGCACHE_TTL_SECONDS", int(_setting(_LANGCACHE, "ttl_seconds")))
