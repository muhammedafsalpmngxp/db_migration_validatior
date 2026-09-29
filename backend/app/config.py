"""Settings, read from backend/.env (the file wins over the machine's environment)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env", override=True)


def _env(key, default=None):
    value = os.getenv(key)
    return value if value not in (None, "") else default


def _flag(key, default):
    return str(_env(key, "yes" if default else "no")).strip().lower() in ("1", "true", "yes", "y")


# ---- SQL Server --------------------------------------------------------------

MSSQL_HOST = _env("MSSQL_HOST", "localhost")
MSSQL_PORT = _env("MSSQL_PORT", "1433")
MSSQL_USER = _env("MSSQL_USER", "")
MSSQL_PASSWORD = _env("MSSQL_PASSWORD", "")
MSSQL_TRUSTED = _flag("MSSQL_TRUSTED", False)
MSSQL_DRIVER = _env("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
MSSQL_ENCRYPT = _flag("MSSQL_ENCRYPT", True)
MSSQL_TRUST_CERT = _flag("MSSQL_TRUST_CERT", True)
# Seconds to wait for a login, and for a single statement.
MSSQL_LOGIN_TIMEOUT = int(_env("MSSQL_LOGIN_TIMEOUT", "20"))
MSSQL_QUERY_TIMEOUT = int(_env("MSSQL_QUERY_TIMEOUT", "60"))
# How long a read waits on a lock held by another session (a load in progress) before
# that table is reported as locked instead of stalling the request.
MSSQL_LOCK_TIMEOUT_MS = int(_env("MSSQL_LOCK_TIMEOUT_MS", "2000"))
# An exact COUNT(*) reads the whole table; the largest source table has ~73M rows.
MSSQL_COUNT_TIMEOUT = int(_env("MSSQL_COUNT_TIMEOUT", "300"))

# ---- The three databases -----------------------------------------------------
# The keys A / B / T are fixed: the mapping file refers to tables as A.dbo.X, B.dbo.X
# and T.schema.x. The label is what the UI shows.

DATABASES = {
    "A": {
        "name": _env("DB_SOURCE_A_NAME", "AppMasterDB_UAT"),
        "label": _env("DB_SOURCE_A_LABEL", "A"),
        "role": "source",
    },
    "B": {
        "name": _env("DB_SOURCE_B_NAME", "AppMasterEngDB_Local"),
        "label": _env("DB_SOURCE_B_LABEL", "B"),
        "role": "source",
    },
    "T": {
        "name": _env("DB_TARGET_NAME", "AlTasnimBI"),
        "label": _env("DB_TARGET_LABEL", "T"),
        "role": "target",
    },
}
SOURCE_SIDES = ("A", "B")
TARGET_SIDE = "T"

MAPPING_FILE = Path(_env("MAPPING_FILE", BACKEND_DIR / "mappings" / "migration_plan.yaml"))

# Data check (value by value comparison, see app/datacheck.py).
# A mapping whose source and target rows add up to more than this is skipped.
DATA_CHECK_MAX_ROWS = int(_env("DATA_CHECK_MAX_ROWS", "5000000"))
# Seconds one data check query may run.
DATA_CHECK_TIMEOUT = int(_env("DATA_CHECK_TIMEOUT", "900"))
# The last result of each mapping, kept across restarts.
DATA_CHECK_FILE = Path(_env("DATA_CHECK_FILE", BACKEND_DIR / ".cache" / "data_checks.json"))

# How long a database's table list is reused before it is read again.
TABLE_CACHE_SECONDS = int(_env("TABLE_CACHE_SECONDS", "60"))


def server():
    """`host,port`; a named instance (host\\INSTANCE) takes no port."""
    if "\\" in MSSQL_HOST or not MSSQL_PORT:
        return MSSQL_HOST
    return f"{MSSQL_HOST},{MSSQL_PORT}"


def dsn(database):
    parts = [
        f"DRIVER={{{MSSQL_DRIVER}}}",
        f"SERVER={server()}",
        f"DATABASE={database}",
        f"Encrypt={'yes' if MSSQL_ENCRYPT else 'no'}",
        f"TrustServerCertificate={'yes' if MSSQL_TRUST_CERT else 'no'}",
        "ApplicationIntent=ReadOnly",
    ]
    if MSSQL_TRUSTED:
        parts.append("Trusted_Connection=yes")
    else:
        parts += [f"UID={MSSQL_USER}", f"PWD={{{MSSQL_PASSWORD.replace('}', '}}')}}}"]
    return ";".join(parts) + ";"
