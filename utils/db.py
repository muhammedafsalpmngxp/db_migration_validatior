"""SQL Server access layer: introspection, row fetching and a guarded SELECT runner.

Talks to Microsoft SQL Server over ODBC (pyodbc). Nothing here talks to an LLM, and
every function returns plain JSON friendly Python so the values can live in the
PocketFlow shared store.
"""
import datetime
import decimal
import re
import uuid

import pyodbc

import config

# Server side guards. LOCK_TIMEOUT is in milliseconds.
LOCK_TIMEOUT_MS = 30000

# Raised by the ODBC driver when a statement runs past the client timeout (HYT00), and by
# SQL Server when a lock wait expires (1222). Callers catch this to degrade gracefully.
TIMEOUT_SQLSTATES = ("HYT00", "HYT01")
LOCK_TIMEOUT_MESSAGE = "Lock request time out"


def is_timeout(exc):
    """True when this exception is a statement or lock timeout rather than a real error."""
    state = exc.args[0] if getattr(exc, "args", None) else ""
    return state in TIMEOUT_SQLSTATES or LOCK_TIMEOUT_MESSAGE in str(exc)


class QueryTimeout(Exception):
    """A read that took too long, with the query that did it."""


# ---------------------------------------------------------------- low level


def _connect(dsn, readonly=True):
    timeout = config.MSSQL_QUERY_TIMEOUT
    conn = pyodbc.connect(dsn, timeout=config.MSSQL_TIMEOUT, autocommit=False)
    conn.timeout = timeout  # per statement, not per connection
    if readonly:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCK_TIMEOUT {LOCK_TIMEOUT_MS}")
            # Nothing here writes and every read is rolled back, so dirty reads are safe
            # and keep the comparison from blocking behind a writer on a busy server.
            level = "READ UNCOMMITTED" if config.MSSQL_DIRTY_READS else "READ COMMITTED"
            cur.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
    return conn


def _query(dsn, query, params=None, max_rows=None):
    """Run one statement and return a list of dicts. Always rolled back.

    `max_rows` reads only that many rows instead of all of them, which is how an agent
    query is capped without rewriting it. A timeout comes back as `QueryTimeout` so a
    caller can retry smaller instead of treating it as a broken query.
    """
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(query, *(params or ()))
            except pyodbc.Error as exc:
                if is_timeout(exc):
                    raise QueryTimeout(
                        f"timed out after {config.MSSQL_QUERY_TIMEOUT}s: "
                        f"{' '.join(query.split())[:120]}"
                    ) from exc
                raise
            if cur.description is None:
                rows = []
            else:
                # An expression with no alias comes back with an empty name; give it a
                # position so the value is not lost when the dict is built.
                cols = [
                    c[0] if c[0] else f"column_{i + 1}"
                    for i, c in enumerate(cur.description)
                ]
                fetched = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
                rows = [dict(zip(cols, row)) for row in fetched]
        conn.rollback()
        return rows
    finally:
        conn.close()


def ping(dsn):
    _query(dsn, "SELECT 1 AS ok")
    return True


CONTROL_CHARS = re.compile(r"[\x00-\x1f]")


def quote_ident(name):
    """Bracket-quote a table/column/schema name, rejecting anything that is not one.

    pyodbc has no `psycopg2.sql.Identifier`, so identifiers coming from the agent are
    validated here before they are ever concatenated into SQL. Real schemas are full of
    delimited names - leading digits, spaces, brackets, dots - and SQL Server accepts any
    of them inside [ ], so the rule is: escape the closing bracket, and refuse only what
    cannot be a name at all (empty, too long, or containing control characters).
    """
    name = str(name)
    if not name or len(name) > 128 or CONTROL_CHARS.search(name):
        raise ValueError(f"Not a valid SQL Server identifier: {name!r}")
    return "[" + name.replace("]", "]]") + "]"


def jsonable(value):
    """Make a SQL Server value safe to store in the shared store / render in Streamlit."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------- introspection


def _render_type(col):
    base = col["data_type"]
    length = col.get("character_maximum_length")
    if length is not None and base not in ("text", "ntext", "image"):
        return f"{base}(max)" if length == -1 else f"{base}({length})"
    if base in ("decimal", "numeric") and col.get("numeric_precision") is not None:
        return f"{base}({col['numeric_precision']},{col.get('numeric_scale') or 0})"
    if base in ("datetime2", "time", "datetimeoffset") and col.get("datetime_precision") is not None:
        return f"{base}({col['datetime_precision']})"
    return base


SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin",
                  "db_securityadmin", "db_ddladmin", "db_backupoperator", "db_datareader",
                  "db_datawriter", "db_denydatareader", "db_denydatawriter")


def qualify(schema, table):
    """`schema.table` - the key every table is known by, since the two databases put the
    same table in different schemas (dbo.task_daily on one side, well.task_daily on the
    other)."""
    return f"{schema}.{table}"


def _fold(name):
    """Lowercase, alphanumerics only - the same notion of "the same name" as pairing."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _schema_list(schema):
    """Normalise the schema argument: None/'' means every user schema."""
    if not schema:
        return None
    if isinstance(schema, str):
        return [p.strip() for p in schema.split(",") if p.strip()]
    return list(schema)


def fetch_schema(dsn, schema=None, with_row_counts=True, only=None):
    """Return {"schema.table": {schema, table, columns, primary_key, indexes, row_count}}.

    `schema` restricts to one or more schemas; None reads every user schema, which is
    what a database that spreads its tables over dbo/well/ref/... needs. `only` accepts
    qualified names ("well.task_daily") or bare ones ("task_daily"). Row counts are the
    expensive part of introspection, so narrowing here - rather than in the caller - is
    what makes a selected-tables run quick on a large schema.
    """
    schemas = _schema_list(schema)
    wanted = set(only) if only else None
    wanted_folded = set()
    for name in wanted or ():
        wanted_folded |= {name, name.lower(), _fold(name)}
        if "." in name:  # a qualified name also matches by its bare part
            wanted_folded.add(name.split(".", 1)[1])

    where = "TABLE_TYPE = 'BASE TABLE'"
    params = []
    if schemas:
        where += " AND TABLE_SCHEMA IN (" + ", ".join("?" * len(schemas)) + ")"
        params += schemas
    else:
        where += " AND TABLE_SCHEMA NOT IN (" + ", ".join("?" * len(SYSTEM_SCHEMAS)) + ")"
        params += list(SYSTEM_SCHEMAS)

    # `only` has to recognise a table by whatever name the caller knows it as: the
    # qualified name, the bare name, either in a different case, or with underscores
    # folded away - because pairing matches `PlantDescription` to `plant_description`,
    # and a selection made on one side's spelling must still find the other side's.
    def keep(schema_name, table_name):
        if wanted is None:
            return True
        candidates = {
            qualify(schema_name, table_name),
            table_name,
            table_name.lower(),
            _fold(table_name),
        }
        return bool(candidates & wanted_folded)

    tables = [
        (r["table_schema"], r["table_name"])
        for r in _query(
            dsn,
            "SELECT TABLE_SCHEMA AS table_schema, TABLE_NAME AS table_name"
            " FROM INFORMATION_SCHEMA.TABLES"
            f" WHERE {where} ORDER BY TABLE_SCHEMA, TABLE_NAME",
            params,
        )
        if keep(r["table_schema"], r["table_name"])
    ]

    columns = _query(
        dsn,
        """SELECT TABLE_SCHEMA AS table_schema, TABLE_NAME AS table_name,
                  COLUMN_NAME AS column_name,
                  ORDINAL_POSITION AS ordinal_position, DATA_TYPE AS data_type,
                  IS_NULLABLE AS is_nullable, COLUMN_DEFAULT AS column_default,
                  CHARACTER_MAXIMUM_LENGTH AS character_maximum_length,
                  NUMERIC_PRECISION AS numeric_precision, NUMERIC_SCALE AS numeric_scale,
                  DATETIME_PRECISION AS datetime_precision
           FROM INFORMATION_SCHEMA.COLUMNS
           ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION""",
    )

    pks = _query(
        dsn,
        """SELECT tc.TABLE_SCHEMA AS table_schema, tc.TABLE_NAME AS table_name,
                  kcu.COLUMN_NAME AS column_name
           FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
           JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
             ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
           WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
           ORDER BY tc.TABLE_SCHEMA, tc.TABLE_NAME, kcu.ORDINAL_POSITION""",
    )

    # sys.indexes is the SQL Server equivalent of pg_indexes; the definition is rebuilt
    # from the index columns so two servers can be compared textually.
    indexes = _query(
        dsn,
        """SELECT s.name AS table_schema, t.name AS table_name, i.name AS index_name,
                  i.type_desc AS index_type,
                  i.is_unique, i.is_primary_key,
                  STUFF((
                      SELECT ', ' + c.name + CASE WHEN ic.is_descending_key = 1
                                                  THEN ' DESC' ELSE '' END
                      FROM sys.index_columns ic
                      JOIN sys.columns c
                        ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                      WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                        AND ic.is_included_column = 0
                      ORDER BY ic.key_ordinal
                      FOR XML PATH('')), 1, 2, '') AS key_columns
           FROM sys.indexes i
           JOIN sys.tables t ON t.object_id = i.object_id
           JOIN sys.schemas s ON s.schema_id = t.schema_id
           WHERE i.name IS NOT NULL
           ORDER BY s.name, t.name, i.name""",
    )

    out = {
        qualify(sch, tab): {
            "schema": sch,
            "table": tab,
            "columns": {},
            "primary_key": [],
            "indexes": [],
            "row_count": None,
        }
        for sch, tab in tables
    }

    for c in columns:
        key = qualify(c["table_schema"], c["table_name"])
        if key not in out:
            continue
        out[key]["columns"][c["column_name"]] = {
            "position": c["ordinal_position"],
            "type": _render_type(c),
            "nullable": c["is_nullable"] == "YES",
            "default": c["column_default"],
        }
    for p in pks:
        key = qualify(p["table_schema"], p["table_name"])
        if key in out:
            out[key]["primary_key"].append(p["column_name"])
    for i in indexes:
        key = qualify(i["table_schema"], i["table_name"])
        if key not in out:
            continue
        kind = "UNIQUE " if i["is_unique"] else ""
        kind += "CLUSTERED" if i["index_type"] == "CLUSTERED" else "NONCLUSTERED"
        out[key]["indexes"].append(
            {
                "name": i["index_name"],
                "definition": f"{kind} ({i['key_columns'] or ''}) ON {i['table_name']}",
            }
        )

    if with_row_counts:
        # One metadata read for the whole database instead of a count(*) scan per table:
        # count(*) on a 71M row table is what used to make a full run take minutes.
        # sys.partitions needs no extra grant (unlike sys.dm_db_partition_stats, which
        # wants VIEW DATABASE PERFORMANCE STATE) and cannot be blocked by a writer. It
        # can lag by a few rows on a busy table, which is why a table whose counts look
        # equal is still compared row by row.
        try:
            counts = {
                (r["table_schema"], r["table_name"]): r["n"]
                for r in _query(
                    dsn,
                    """SELECT s.name AS table_schema, t.name AS table_name,
                              SUM(p.rows) AS n
                       FROM sys.partitions p
                       JOIN sys.tables t ON t.object_id = p.object_id
                       JOIN sys.schemas s ON s.schema_id = t.schema_id
                       WHERE p.index_id IN (0, 1)
                       GROUP BY s.name, t.name""",
                )
            }
        except Exception:  # no metadata visibility, older server, ...
            counts = {}

        for sch, tab in tables:
            key = qualify(sch, tab)
            if (sch, tab) in counts:
                out[key]["row_count"] = counts[(sch, tab)]
                continue
            q = f"SELECT count(*) AS n FROM {quote_ident(sch)}.{quote_ident(tab)}"
            try:
                out[key]["row_count"] = _query(dsn, q)[0]["n"]
            except QueryTimeout:
                out[key]["row_count"] = None  # unknown beats failing the whole run

    return out


# ---------------------------------------------------------------- data access


def fetch_rows(dsn, table, schema="dbo", order_by=None, limit=5000):
    order = order_by or []
    q = (
        f"SELECT TOP ({int(limit)}) * "
        f"FROM {quote_ident(schema)}.{quote_ident(table)}"
    )
    if order:
        q += " ORDER BY " + ", ".join(quote_ident(c) for c in order)
    return [jsonable(r) for r in _query(dsn, q)]


FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|exec|execute|"
    r"sp_\w+|xp_\w+|bulk|openrowset|opendatasource|backup|restore|shutdown|waitfor|"
    r"dbcc|reconfigure|into)\b",
    re.IGNORECASE,
)


def run_select(dsn, query, limit=200):
    """Run an agent supplied read only query. Raises on anything that is not a single SELECT."""
    q = query.strip().rstrip(";").strip()
    if ";" in q:
        raise ValueError("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", q, re.IGNORECASE):
        raise ValueError("Only SELECT / WITH queries are allowed.")
    if FORBIDDEN.search(q):
        raise ValueError("Query contains a write, DDL or procedure keyword.")

    # The query runs exactly as written and the cap is applied while reading the rows.
    # Wrapping it in `SELECT TOP (n) * FROM (...) AS sub` used to be the cap, but a
    # derived table imposes rules the agent has no reason to expect: every column needs
    # a name, so `SELECT project_id, COUNT(*) ... GROUP BY project_id` failed with
    # "No column name was specified for column 2", and ORDER BY was rejected outright.
    # Those were our constraints leaking into its SQL. Reading only `limit` rows costs
    # nothing extra and lets the model write ordinary T-SQL.
    return [jsonable(r) for r in _query(dsn, q, max_rows=int(limit))]
