"""Read-only connections to the ATNM and RDS servers, and plain-words connection errors."""
import pyodbc

from . import settings


class NotConfigured(Exception):
    """The server has no host in backend/.env."""


class Locked(Exception):
    """A table is held by another session (a load in progress?)."""


def connect(server, database, timeout=None):
    """A read-only, autocommit connection that reads uncommitted and gives up on a lock
    after ATNM_LOCK_TIMEOUT_MS instead of waiting for it."""
    if not server.host:
        raise NotConfigured(f"No host is set for the {server.label} server.")
    con = pyodbc.connect(server.dsn(database), timeout=server.login_timeout, readonly=True, autocommit=True)
    con.timeout = timeout or settings.QUERY_TIMEOUT
    cur = con.cursor()
    cur.execute(f"SET LOCK_TIMEOUT {int(settings.LOCK_TIMEOUT_MS)}")
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
    return con


def reachable(server, database):
    """True when a login to the database works right now (a quick test: SELECT 1)."""
    try:
        con = connect(server, database, timeout=30)
        try:
            con.cursor().execute("SELECT 1").fetchone()
        finally:
            con.close()
        return True
    except Exception:
        return False


def is_lock_timeout(exc):
    text = str(exc)
    return "1222" in text or "Lock request time out" in text


def fetch(cur, sql, params=()):
    """Rows of one query as dicts. User input only ever arrives through `params`."""
    try:
        cur.execute(sql, params) if params else cur.execute(sql)
    except pyodbc.Error as exc:
        if is_lock_timeout(exc):
            raise Locked("A table is locked by another session (a load in progress?).") from exc
        raise
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def kind(exc):
    """What sort of failure this is, which decides what a run does next:

    connection  the server cannot be reached (VPN down, network)   -> wait and retry
    timeout     a query ran longer than ATNM_QUERY_TIMEOUT          -> report, go on
    locked      a table is held by another session                  -> try again at the end
    changed     the table or a column disappeared during the check  -> report, go on
    login       the login was refused                               -> stop the run
    config      no host, missing driver, unknown database           -> stop the run
    other       anything else                                       -> report, go on
    """
    if isinstance(exc, NotConfigured):
        return "config"
    if isinstance(exc, Locked):
        return "locked"
    text = str(exc)
    low = text.lower()
    if "query timeout" in low:
        return "timeout"
    if "im002" in low or "4060" in text or "cannot open database" in low:
        return "config"
    if "18456" in text or "login failed" in low:
        return "login"
    if "42s02" in low or "42s22" in low or "invalid object name" in low or "invalid column name" in low:
        return "changed"
    if ("08001" in text or "08s01" in low or "hyt00" in low or "login timeout" in low
            or "not found or was not accessible" in low or "tcp provider" in low or "network-related" in low
            or "communication link failure" in low or "connection is busy" in low or "10054" in text):
        return "connection"
    return "other"


def explain(exc, server, database=None):
    """(message, hint) for a failed connection or query, in plain words."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, NotConfigured):
        env = "ATNM_MSSQL_HOST" if server.key == "source" else "MSSQL_HOST"
        return text, f"Set {env} (and its login) in backend/.env, then restart the backend."
    if isinstance(exc, Locked):
        return text, "Try again once the load has finished."
    if "query timeout" in low:
        return (f"A query on the {server.label} server ran longer than ATNM_QUERY_TIMEOUT "
                f"({settings.QUERY_TIMEOUT:,} s).", "Raise ATNM_QUERY_TIMEOUT in backend/.env, or check this table "
                "at a quieter time.")
    if "42s02" in low or "42s22" in low or "invalid object name" in low or "invalid column name" in low:
        return (f"The table changed on the {server.label} server during the check (a table or column is gone).",
                "Refresh, then check the table again.")
    if "im002" in low:
        return (f"The ODBC driver '{server.driver}' is not installed on this machine.",
                "Install it, or set the driver that is installed (see pyodbc.drivers()) in backend/.env.")
    if "18456" in text or "login failed" in low:
        env = "ATNM_MSSQL_USER / ATNM_MSSQL_PASSWORD" if server.key == "source" else "MSSQL_USER / MSSQL_PASSWORD"
        return f"The {server.label} server refused the login.", f"Check {env} in backend/.env."
    if "4060" in text or "cannot open database" in low:
        return (f"The database {database or ''} cannot be opened on the {server.label} server.",
                "Check the database name in backend/.env, and that the login has access to it.")
    if ("08001" in text or "hyt00" in low or "login timeout" in low or "not found or was not accessible" in low
            or "tcp provider" in low or "network-related" in low or "08s01" in low):
        hint = ("Connect the VPN, then press Retry." if server.key == "source"
                else "Check the network connection to the server, then press Retry.")
        return f"The {server.label} server ({server.host}) cannot be reached.", hint
    if "ssl" in low or "certificate" in low or "encryption" in low:
        prefix = "ATNM_" if server.key == "source" else ""
        return (f"The encrypted connection to the {server.label} server failed.",
                f"Check {prefix}MSSQL_ENCRYPT and {prefix}MSSQL_TRUST_CERT in backend/.env.")
    return text, None
