"""The toolbox the agent picks from (the 'tool-database' pattern from the cookbooks).

Every tool returns (summary_text, raw_result):
  summary_text goes back into the agent's context, so it stays short;
  raw_result is kept in the shared store for the UI and the final report.
"""
import config
from utils import db, differ

TOOL_CATALOG = """
- compare_schema
    params: {}
    Re-read both schemas and return the full structural diff (tables, columns, types,
    nullability, defaults, primary keys, indexes).

- compare_row_counts
    params: {}
    Row count of every common table on both sides. Cheap, good first move.

- compare_table_data
    params: {table: <table name>, limit: <int, optional, default 5000>}
    Primary-key based row comparison of one common table: rows missing on either
    side and per-column value changes. The strongest evidence of data drift.

- sample_rows
    params: {database: A|B, table: <table name>, limit: <int, optional, default 10>}
    Look at actual rows from one side when you need to understand a column's content.

- run_sql
    params: {database: A|B|both, sql: <single read-only SELECT>}
    Aggregate checks you cannot express with the tools above, e.g.
    SELECT status, count(*) FROM orders GROUP BY status.
    Rejected unless it is a single SELECT/WITH statement.

- finish
    params: {}
    Stop investigating; the evidence is enough to write the report.
"""

VALID_TOOLS = {
    "compare_schema",
    "compare_row_counts",
    "compare_table_data",
    "sample_rows",
    "run_sql",
    "finish",
}


def _dsn_for(which):
    return config.dsn_a() if str(which).upper().startswith("A") else config.dsn_b()


def _label(which):
    return config.DB_A_LABEL if str(which).upper().startswith("A") else config.DB_B_LABEL


def execute_tool(tool, params, shared):
    params = params or {}
    schema = config.SCHEMA

    if tool == "compare_schema":
        sa = db.fetch_schema(config.dsn_a(), schema)
        sb = db.fetch_schema(config.dsn_b(), schema)
        diff = differ.diff_schemas(sa, sb)
        shared["schema"] = {"a": sa, "b": sb}
        shared["schema_diff"] = diff
        text = differ.summarize_schema_diff(diff, config.DB_A_LABEL, config.DB_B_LABEL)
        return text, diff

    if tool == "compare_row_counts":
        diff = shared.get("schema_diff") or {}
        rows = []
        for t in diff.get("common_tables", []):
            d = diff["table_diffs"][t]
            flag = "" if d["row_count_a"] == d["row_count_b"] else "   <-- differs"
            rows.append(f"  {t}: {d['row_count_a']} vs {d['row_count_b']}{flag}")
        result = {"row_counts": diff.get("row_count_mismatches", [])}
        return "Row counts (A vs B):\n" + ("\n".join(rows) or "  no common tables"), result

    if tool == "compare_table_data":
        table = params.get("table")
        if not table:
            raise ValueError("compare_table_data needs a 'table' param")
        diff = shared.get("schema_diff") or {}
        if table not in diff.get("common_tables", []):
            raise ValueError(f"'{table}' is not a table present in both databases")
        limit = int(params.get("limit") or config.ROW_COMPARE_LIMIT)
        pk = diff["table_diffs"][table]["primary_key_a"]
        if not pk:
            return f"{table} has no primary key, a row level comparison is not possible.", {
                "table": table,
                "error": "no primary key",
            }
        rows_a = db.fetch_rows(config.dsn_a(), table, schema, order_by=pk, limit=limit)
        rows_b = db.fetch_rows(config.dsn_b(), table, schema, order_by=pk, limit=limit)
        res = differ.diff_rows(rows_a, rows_b, pk)
        res["table"] = table
        shared.setdefault("data_diffs", {})[table] = res

        lines = [
            f"Row comparison of {table} on primary key {pk}:",
            f"  rows: {res['rows_a']} in A, {res['rows_b']} in B, {res['matching_rows']} identical",
            f"  only in A: {res['only_in_a_count']}, only in B: {res['only_in_b_count']},"
            f" modified: {res['modified_count']}",
        ]
        for m in res["modified"][:5]:
            changed = ", ".join(
                f"{c}: {v['a']} -> {v['b']}" for c, v in list(m["changes"].items())[:4]
            )
            lines.append(f"  changed {m['key']}: {changed}")
        for k in res["only_in_a"][:5]:
            lines.append(f"  missing in B: {k}")
        for k in res["only_in_b"][:5]:
            lines.append(f"  missing in A: {k}")
        return "\n".join(lines), res

    if tool == "sample_rows":
        which = params.get("database", "A")
        table = params.get("table")
        limit = min(int(params.get("limit") or 10), 50)
        rows = db.fetch_rows(_dsn_for(which), table, schema, limit=limit)
        head = f"{limit} sample rows from {table} in {_label(which)}:"
        body = "\n".join(f"  {r}" for r in rows[:limit])
        return f"{head}\n{body}", {"database": which, "table": table, "rows": rows}

    if tool == "run_sql":
        which = str(params.get("database", "both")).lower()
        query = params.get("sql")
        if not query:
            raise ValueError("run_sql needs a 'sql' param")
        targets = ["A", "B"] if which.startswith("bo") else [which.upper()[:1]]
        out, lines = {}, []
        for t in targets:
            rows = db.run_select(_dsn_for(t), query, limit=int(params.get("limit") or 200))
            out[t] = rows
            lines.append(f"{_label(t)} ({len(rows)} rows):")
            lines += [f"  {r}" for r in rows[:15]]
        return "\n".join(lines), {"sql": query, "results": out}

    raise ValueError(f"Unknown tool: {tool}")
