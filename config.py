"""Central configuration. Everything is overridable through environment variables (.env)."""
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional
    pass


def _env(key, default):
    v = os.getenv(key)
    return v if v not in (None, "") else default


PG_HOST = _env("PG_HOST", "localhost")
PG_PORT = _env("PG_PORT", "5432")
PG_USER = _env("PG_USER", "postgres")
PG_PASSWORD = _env("PG_PASSWORD", "postgres")

# The two databases being compared. A = source / baseline, B = target / candidate.
DB_A_NAME = _env("DB_A_NAME", "shop_prod")
DB_B_NAME = _env("DB_B_NAME", "shop_stage")
DB_A_LABEL = _env("DB_A_LABEL", "A (prod)")
DB_B_LABEL = _env("DB_B_LABEL", "B (stage)")
SCHEMA = _env("PG_SCHEMA", "public")

# LLM: openai | anthropic | mock  ("mock" runs the whole agent offline, used by the tests)
LLM_PROVIDER = _env("LLM_PROVIDER", "openai").lower()
LLM_MODEL = _env("LLM_MODEL", "gpt-4o")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", None)
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "4000"))

# Agent limits
MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "8"))
MAX_SUPERVISOR_RETRIES = int(_env("MAX_SUPERVISOR_RETRIES", "1"))
ROW_COMPARE_LIMIT = int(_env("ROW_COMPARE_LIMIT", "5000"))


def dsn(db_name):
    return f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} dbname={db_name}"


def admin_dsn():
    """Connection to the maintenance database, used to CREATE/DROP the test databases."""
    return dsn("postgres")


def dsn_a():
    return dsn(DB_A_NAME)


def dsn_b():
    return dsn(DB_B_NAME)
