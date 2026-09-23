"""Central configuration. Everything is overridable through environment variables (.env)."""
import os

# This project's .env wins over whatever the machine happens to export. A stale
# OPENAI_API_KEY in the user's system environment silently beat the one in .env - two
# different keys, and no way to tell from the outside which one a run used. `override`
# settles it: the file next to the code is the configuration of record.
#
# The guard matters because Streamlit reloads this module after writing the sidebar's
# values into the environment; without it, .env would immediately overwrite the choices
# just made in the UI. So: the file beats the machine, and the sidebar beats both.
_LOADED_FLAG = "DBCOMPARE_ENV_LOADED"

if not os.environ.get(_LOADED_FLAG):
    try:
        from dotenv import load_dotenv

        # The file next to this module, named explicitly: dotenv's search would otherwise
        # walk up the tree and could load a .env belonging to some parent directory.
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                    override=True)
    except ImportError:  # dotenv is optional
        pass
    os.environ[_LOADED_FLAG] = "1"


def _env(key, default):
    v = os.getenv(key)
    return v if v not in (None, "") else default


def _flag(key, default=False):
    return str(_env(key, "yes" if default else "no")).strip().lower() in ("1", "true", "yes", "y")


# ---- Microsoft SQL Server -------------------------------------------------

MSSQL_HOST = _env("MSSQL_HOST", "localhost")
MSSQL_PORT = _env("MSSQL_PORT", "1433")
MSSQL_USER = _env("MSSQL_USER", "sa")
MSSQL_PASSWORD = _env("MSSQL_PASSWORD", "")
# Windows / integrated authentication instead of a SQL login.
MSSQL_TRUSTED = _flag("MSSQL_TRUSTED", False)
def _best_driver():
    """Pick the newest SQL Server ODBC driver installed, when .env does not name one.

    Preference order: ODBC Driver 18 > 17 > 13 > the legacy 'SQL Server' driver.
    """
    try:
        import pyodbc

        installed = [d for d in pyodbc.drivers() if "SQL Server" in d]
    except Exception:
        installed = []
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                      "ODBC Driver 13 for SQL Server", "SQL Server Native Client 11.0"):
        if candidate in installed:
            return candidate
    return installed[0] if installed else "ODBC Driver 17 for SQL Server"


MSSQL_DRIVER = _env("MSSQL_DRIVER", None) or _best_driver()
# ODBC Driver 18 encrypts by default and then rejects a self-signed dev certificate.
MSSQL_ENCRYPT = _flag("MSSQL_ENCRYPT", True)
MSSQL_TRUST_CERT = _flag("MSSQL_TRUST_CERT", True)
MSSQL_TIMEOUT = int(_env("MSSQL_TIMEOUT", "30"))
# Two different waits: MSSQL_TIMEOUT is how long to wait for a connection, this is how
# long a single statement may take. Reading a wide table on a busy server needs more than
# the login does.
MSSQL_QUERY_TIMEOUT = int(_env("MSSQL_QUERY_TIMEOUT", "120"))
# Reads never write and always roll back, so READ UNCOMMITTED by default: a comparison
# should not queue behind whoever is loading the database. Set no to read committed data.
MSSQL_DIRTY_READS = _flag("MSSQL_DIRTY_READS", True)

# The two databases being compared. A = source / baseline, B = target / candidate.
DB_A_NAME = _env("DB_A_NAME", "AppMasterDB_UAT")
DB_B_NAME = _env("DB_B_NAME", "AlTasnimBI")
DB_A_LABEL = _env("DB_A_LABEL", "A (AppMasterDB_UAT)")
DB_B_LABEL = _env("DB_B_LABEL", "B (AlTasnimBI)")
# Empty (the default) reads every user schema, which is what a database that spreads its
# tables over dbo/well/ref/... needs. Set one name, or a comma separated list, to narrow.
SCHEMA = _env("MSSQL_SCHEMA", "") or None

# LLM: openai | anthropic | mock  ("mock" runs the whole agent offline, used by the tests)
LLM_PROVIDER = _env("LLM_PROVIDER", "openai").lower()
LLM_MODEL = _env("LLM_MODEL", "gpt-4o")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", None)
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "4000"))

# Agent limits
MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "8"))
MAX_SUPERVISOR_RETRIES = int(_env("MAX_SUPERVISOR_RETRIES", "1"))
ROW_COMPARE_LIMIT = int(_env("ROW_COMPARE_LIMIT", "5000"))
# A row by row comparison reads both sides in full and diffs them in memory. Past this
# many rows that is neither quick nor useful (AppMasterDB_UAT has a 71M row table), so
# the tool reports the counts and says what to do instead, unless asked with force: true.
ROW_COMPARE_MAX_ROWS = int(_env("ROW_COMPARE_MAX_ROWS", "200000"))
# An aggregate with no WHERE reads every row. Over this many, it will not come back
# inside the statement timeout, so run_sql asks for a range instead of trying.
SQL_SCAN_MAX_ROWS = int(_env("SQL_SCAN_MAX_ROWS", "2000000"))


def server():
    """`host,port`, the form the ODBC driver expects (a named instance needs no port)."""
    host = MSSQL_HOST
    return host if "\\" in host or not MSSQL_PORT else f"{host},{MSSQL_PORT}"


def dsn(db_name):
    """An ODBC connection string for one database."""
    parts = [
        f"DRIVER={{{MSSQL_DRIVER}}}",
        f"SERVER={server()}",
        f"DATABASE={db_name}",
    ]
    # The legacy 'SQL Server' driver shipped with Windows rejects these keywords outright
    # ("Invalid connection string attribute"); pyodbc's own `timeout=` covers the login wait.
    if MSSQL_DRIVER.lower().startswith("odbc driver"):
        parts += [
            f"Encrypt={'yes' if MSSQL_ENCRYPT else 'no'}",
            f"TrustServerCertificate={'yes' if MSSQL_TRUST_CERT else 'no'}",
            f"Connection Timeout={MSSQL_TIMEOUT}",
        ]
    if MSSQL_TRUSTED:
        parts.append("Trusted_Connection=yes")
    else:
        parts += [f"UID={MSSQL_USER}", f"PWD={MSSQL_PASSWORD}"]
    return ";".join(parts) + ";"


def admin_dsn():
    """Connection to `master`, used to CREATE/DROP the test databases."""
    return dsn("master")


def dsn_a():
    return dsn(DB_A_NAME)


def dsn_b():
    return dsn(DB_B_NAME)
