"""Settings of the ATNM copy check, read from backend/.env (app.config loads the file).

Two servers take part:
  source  the client's ATNM server, reached over the VPN (ATNM_MSSQL_*)
  target  the RDS server the databases were copied to - the same server the rest of the
          app uses (MSSQL_*)

and a list of database pairs, ATNM_DB_<n>_SOURCE -> ATNM_DB_<n>_TARGET, each an ATNM
database and its copy on RDS. Nothing about tables, columns or row counts is configured:
every table of each database is read from the server.
"""
from dataclasses import dataclass
from pathlib import Path

from .. import config

_env, _flag = config._env, config._flag


@dataclass(frozen=True)
class Server:
    key: str            # "source" | "target"
    label: str          # what the UI shows
    host: str
    port: str
    user: str
    password: str
    trusted: bool
    driver: str
    encrypt: bool
    trust_cert: bool
    login_timeout: int

    def address(self):
        """`host,port`; a named instance (host\\INSTANCE) takes no port."""
        if "\\" in self.host or not self.port:
            return self.host
        return f"{self.host},{self.port}"

    def dsn(self, database):
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.address()}",
            f"DATABASE={database}",
            f"Encrypt={'yes' if self.encrypt else 'no'}",
            f"TrustServerCertificate={'yes' if self.trust_cert else 'no'}",
            "ApplicationIntent=ReadOnly",
        ]
        if self.trusted:
            parts.append("Trusted_Connection=yes")
        else:
            parts += [f"UID={self.user}", f"PWD={{{self.password.replace('}', '}}')}}}"]
        return ";".join(parts) + ";"

    def public(self):
        """What the API may show about the server: never the login."""
        return {"key": self.key, "label": self.label, "host": self.host, "configured": bool(self.host)}


@dataclass(frozen=True)
class Pair:
    id: str
    source_db: str      # database on the ATNM server
    target_db: str      # its copy on the RDS server

    def public(self):
        return {"id": self.id, "source_db": self.source_db, "target_db": self.target_db}


SOURCE = Server(
    key="source",
    label=_env("ATNM_LABEL", "ATNM"),
    host=_env("ATNM_MSSQL_HOST", ""),
    port=_env("ATNM_MSSQL_PORT", "1433"),
    user=_env("ATNM_MSSQL_USER", ""),
    password=_env("ATNM_MSSQL_PASSWORD", ""),
    trusted=_flag("ATNM_MSSQL_TRUSTED", False),
    driver=_env("ATNM_MSSQL_DRIVER", config.MSSQL_DRIVER),
    encrypt=_flag("ATNM_MSSQL_ENCRYPT", True),
    trust_cert=_flag("ATNM_MSSQL_TRUST_CERT", True),
    login_timeout=int(_env("ATNM_LOGIN_TIMEOUT", "15")),
)

TARGET = Server(
    key="target",
    label=_env("ATNM_TARGET_LABEL", "RDS"),
    host=config.MSSQL_HOST,
    port=config.MSSQL_PORT,
    user=config.MSSQL_USER,
    password=config.MSSQL_PASSWORD,
    trusted=config.MSSQL_TRUSTED,
    driver=config.MSSQL_DRIVER,
    encrypt=config.MSSQL_ENCRYPT,
    trust_cert=config.MSSQL_TRUST_CERT,
    login_timeout=config.MSSQL_LOGIN_TIMEOUT,
)

SERVERS = {"source": SOURCE, "target": TARGET}


def _pairs():
    """ATNM_DB_1_SOURCE / ATNM_DB_1_TARGET, ATNM_DB_2_..., in order; a gap ends the list."""
    out = []
    for n in range(1, 51):
        src, tgt = _env(f"ATNM_DB_{n}_SOURCE"), _env(f"ATNM_DB_{n}_TARGET")
        if not src and not tgt:
            break
        if src and tgt:
            out.append(Pair(str(n), src, tgt))
    return out


PAIRS = _pairs()

# Seconds one data query may run: one pass over a table of tens of millions of rows takes
# most of an hour.
QUERY_TIMEOUT = int(_env("ATNM_QUERY_TIMEOUT", "7200"))
# How long a read waits on a lock held by another session before the table is reported locked.
LOCK_TIMEOUT_MS = int(_env("ATNM_LOCK_TIMEOUT_MS", "5000"))
# Most rows per side fetched to find which rows differ (keys and hashes only).
DIFF_ROWS_MAX = int(_env("ATNM_DIFF_ROWS_MAX", "100000"))
# Tables above this many rows are not value-checked; 0 means no limit.
DATA_CHECK_MAX_ROWS = int(_env("ATNM_DATA_CHECK_MAX_ROWS", "0"))
# Renamed columns are found by their data with the rules of the RDS rename check (the same
# RENAME_* settings): identical on every paired row, enough filled values, more than one
# value, and enough of the rows paired. Rows are paired on the row key (or on the columns
# both tables share), reading keys and hashes only, up to ATNM_DIFF_ROWS_MAX rows per side.
RENAME_MIN_VALUES = int(_env("RENAME_MIN_VALUES", "100"))
RENAME_MIN_COVERAGE = float(_env("RENAME_MIN_COVERAGE", "0.5"))
RENAME_POSSIBLE_MIN = float(_env("RENAME_POSSIBLE_MIN", "0.5"))
# How long the table lists of a database are reused before they are read again.
CACHE_SECONDS = int(_env("ATNM_CACHE_SECONDS", str(config.TABLE_CACHE_SECONDS)))
RESULT_FILE = Path(_env("ATNM_RESULT_FILE", config.BACKEND_DIR / ".cache" / "atnm_checks.json"))
# Every step of every check, also shown in the backend console (see app/ATNM/activity.py).
LOG_FILE = Path(_env("ATNM_LOG_FILE", config.BACKEND_DIR / ".cache" / "atnm.log"))
# The run in progress (its tables, and which are done), so it can be resumed after a restart.
RUN_FILE = Path(_env("ATNM_RUN_FILE", config.BACKEND_DIR / ".cache" / "atnm_run.json"))
# Optional per-table settings, such as a cutoff for live tables (see app/ATNM/options.py).
OPTIONS_FILE = Path(_env("ATNM_OPTIONS_FILE", config.BACKEND_DIR / "mappings" / "atnm.yaml"))

# A lost connection (VPN down) is tried again after these waits, in seconds; then the run
# pauses and tries every ATNM_PAUSE_POLL seconds until the server can be reached again.
RETRY_WAITS = [int(x) for x in str(_env("ATNM_RETRY_WAITS", "30,60,120")).split(",") if x.strip()]
PAUSE_POLL = int(_env("ATNM_PAUSE_POLL", "30"))


def pair(pair_id):
    return next((p for p in PAIRS if p.id == str(pair_id)), None)
