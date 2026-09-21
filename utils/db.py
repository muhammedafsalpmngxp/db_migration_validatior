"""Postgres access layer: introspection, row fetching and a guarded SELECT runner.

Nothing here talks to an LLM. Every function returns plain JSON friendly Python so
the values can live in the PocketFlow shared store.
"""
import datetime
import decimal
import re

import psycopg2
import psycopg2.extras
from psycopg2 import sql

STATEMENT_TIMEOUT = "30s"


# ---------------------------------------------------------------- low level


def _query(dsn, query, params=None, readonly=True):
    conn = psycopg2.connect(dsn)
    try:
        if readonly:
            conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            cur.execute(query, params or ())
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        conn.rollback()
        return rows
    finally:
        conn.close()


def ping(dsn):
    _query(dsn, "SELECT 1 AS ok")
    return True


def jsonable(value):
    """Make a Postgres value safe to store in the shared store / render in Streamlit."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------- introspection


def _render_type(col):
    base = col["data_type"]
    if col.get("character_maximum_length"):
        return f"{base}({col['character_maximum_length']})"
    if base == "numeric" and col.get("numeric_precision") is not None:
        return f"numeric({col['numeric_precision']},{col.get('numeric_scale') or 0})"
    return base


def fetch_schema(dsn, schema="public", with_row_counts=True):
    """Return {table: {columns, primary_key, indexes, row_count}} for one database."""
    tables = [
        r["table_name"]
        for r in _query(
            dsn,
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = %s AND table_type = 'BASE TABLE'
               ORDER BY table_name""",
            (schema,),
        )
    ]

    columns = _query(
        dsn,
        """SELECT table_name, column_name, ordinal_position, data_type, is_nullable,
                  column_default, character_maximum_length, numeric_precision, numeric_scale
           FROM information_schema.columns
           WHERE table_schema = %s
           ORDER BY table_name, ordinal_position""",
        (schema,),
    )

    pks = _query(
        dsn,
        """SELECT tc.table_name, kcu.column_name, kcu.ordinal_position
           FROM information_schema.table_constraints tc
           JOIN information_schema.key_column_usage kcu
             ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
           WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
           ORDER BY tc.table_name, kcu.ordinal_position""",
        (schema,),
    )

    indexes = _query(
        dsn,
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = %s"
        " ORDER BY tablename, indexname",
        (schema,),
    )

    out = {t: {"columns": {}, "primary_key": [], "indexes": [], "row_count": None} for t in tables}

    for c in columns:
        if c["table_name"] not in out:
            continue
        out[c["table_name"]]["columns"][c["column_name"]] = {
            "position": c["ordinal_position"],
            "type": _render_type(c),
            "nullable": c["is_nullable"] == "YES",
            "default": c["column_default"],
        }
    for p in pks:
        if p["table_name"] in out:
            out[p["table_name"]]["primary_key"].append(p["column_name"])
    for i in indexes:
        if i["tablename"] in out:
            out[i["tablename"]]["indexes"].append(
                {"name": i["indexname"], "definition": i["indexdef"]}
            )

    if with_row_counts:
        for t in tables:
            q = sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(t)
            )
            out[t]["row_count"] = _query(dsn, q)[0]["n"]

    return out


# ---------------------------------------------------------------- data access


def fetch_rows(dsn, table, schema="public", order_by=None, limit=5000):
    order = order_by or []
    q = sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    if order:
        q = q + sql.SQL(" ORDER BY ") + sql.SQL(", ").join(sql.Identifier(c) for c in order)
    q = q + sql.SQL(" LIMIT {}").format(sql.Literal(int(limit)))
    return [jsonable(r) for r in _query(dsn, q)]


FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|"
    r"call|do|merge|comment|reindex|refresh)\b",
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
        raise ValueError("Query contains a write or DDL keyword.")
    wrapped = sql.SQL("SELECT * FROM ({}) AS _agent_sub LIMIT {}").format(
        sql.SQL(q), sql.Literal(int(limit))
    )
    return [jsonable(r) for r in _query(dsn, wrapped)]
