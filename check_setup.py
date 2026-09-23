"""Preflight check: SQL Server reachable, sample data present, LLM key working.

    uv run check_setup.py
"""
import os
import sys

import config
from utils import db
from utils.call_llm import call_llm

OK, BAD = "  [ok]  ", "  [fail]"


def check_driver():
    """The ODBC driver is installed separately from the Python package."""
    import pyodbc

    available = [d for d in pyodbc.drivers() if "SQL Server" in d]
    print("ODBC driver")
    if config.MSSQL_DRIVER in available:
        print(f"{OK} {config.MSSQL_DRIVER}")
        return True
    print(f"{BAD} {config.MSSQL_DRIVER!r} is not installed")
    print(f"         installed SQL Server drivers: {', '.join(available) or 'none'}")
    print("         install 'ODBC Driver 18 for SQL Server' from Microsoft, or set")
    print("         MSSQL_DRIVER in .env to one of the names listed above")
    return False


def check_sqlserver():
    auth = "Windows auth" if config.MSSQL_TRUSTED else f"login {config.MSSQL_USER}"
    print(f"\nSQL Server {config.server()} ({auth})")
    healthy = True
    for label, name, dsn in (
        (config.DB_A_LABEL, config.DB_A_NAME, config.dsn_a()),
        (config.DB_B_LABEL, config.DB_B_NAME, config.dsn_b()),
    ):
        try:
            schema = db.fetch_schema(dsn, config.SCHEMA, with_row_counts=False)
            print(f"{OK} {label}: {name} reachable, {len(schema)} tables"
                  f" ({', '.join(sorted(schema)) or 'empty'})")
            if not schema:
                print("         database is empty - run: uv run setup_test_data.py")
                healthy = False
        except Exception as exc:
            print(f"{BAD} {label}: {name} - {exc}")
            healthy = False
    return healthy


def check_llm():
    provider = os.getenv("LLM_PROVIDER", config.LLM_PROVIDER)
    model = os.getenv("LLM_MODEL", config.LLM_MODEL)
    key = os.getenv("OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY", "")
    shown = f"...{key[-6:]}" if key else "not set"
    print(f"\nLLM  provider={provider}  model={model}  key={shown}")
    try:
        reply = call_llm(
            "Reply with exactly the three characters: ok. No punctuation, no explanation."
        )
        print(f"{OK} responded: {reply.strip()[:80]}")
        return True
    except Exception as exc:
        print(f"{BAD} {type(exc).__name__}: {exc}")
        if provider == "openai":
            print("         check OPENAI_API_KEY in .env, and that LLM_MODEL is a model")
            print("         your key can reach (gpt-4o, gpt-4o-mini, ...)")
        return False


if __name__ == "__main__":
    ok = check_driver()
    ok = check_sqlserver() and ok
    ok = check_llm() and ok
    print("\nReady to run: uv run streamlit run app.py" if ok else "\nFix the failures above.")
    sys.exit(0 if ok else 1)
