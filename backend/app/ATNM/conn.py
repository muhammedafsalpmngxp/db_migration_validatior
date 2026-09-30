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


def explain(exc, server, database=None):
    """(message, hint) for a failed connection or query, in plain words."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, NotConfigured):
        env = "ATNM_MSSQL_HOST" if server.key == "source" else "MSSQL_HOST"
        return text, f"Set {env} (and its login) in backend/.env, then restart the backend."
    if isinstance(exc, Locked):
        return text, "Try again once the load has finished."
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
