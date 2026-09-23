"""The toolbox the agent picks from (the 'tool-database' pattern from the cookbooks).

Every tool returns (summary_text, raw_result):
  summary_text goes back into the agent's context, so it stays short;
  raw_result is kept in the shared store for the UI and the final report.
"""
import re

import config
from utils import db, differ, toon

# Never degrade below this many rows: past it the comparison stops being informative.
MIN_ROW_LIMIT = 200
# Character budgets for row payloads in an observation: they are re-sent on every later
# call, so they are capped - but as whole rows, not by cutting each row in half.
SAMPLE_CHARS = 4000
RESULT_CHARS = 4000

TOOL_CATALOG = """
- compare_schema
    params: {}
    Re-read both schemas and return the full structural diff (tables, columns, types,
    nullability, defaults, primary keys, indexes).

- compare_row_counts
    params: {}
    Row count of every common table on both sides. Cheap, good first move.

- compare_table_data
    params: {table: <table name>, limit: <int, optional, default 5000>,
             force: <true, optional>}
    Primary-key based row comparison of one matched table: rows missing on either
    side and per-column value changes. The strongest evidence of data drift.
    Needs a primary key, and refuses a table over 200k rows unless force: true,
    which compares the first `limit` rows by primary key only. For a table that
    big, prefer run_sql with an aggregate (counts grouped by a status or date).

- sample_rows
    params: {database: A|B, table: <table name>, limit: <int, optional, default 10>}
    Look at actual rows from one side when you need to understand a column's content.

- run_sql
    params: {database: A|B|both, sql: <single read-only SELECT>, force: <true, optional>}
    Aggregate checks you cannot express with the tools above, e.g.
    SELECT status, count(*) FROM orders GROUP BY status.
    Write table names bare, as they appear in this diff: each side resolves them to
    its own schema, so the same SQL runs against both databases. Column names are
    checked against the real schema before the query runs, and a wrong one comes back
    with the list of columns that table actually has. So is the type: SUM or AVG over a
    column that is text on one side is refused with both types, rather than scanning the
    table and failing.
    T-SQL (Microsoft SQL Server): use TOP (n) instead of LIMIT and GETDATE() instead
    of now(). ORDER BY is fine, and so is an unaliased expression - the query runs as
    written. Rejected unless it is a single SELECT/WITH statement.
    An aggregate over a table of tens of millions of rows is still a full scan, so
    restrict it with a WHERE on a date or key range when the table is that large.

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


SQL_OBJECT = re.compile(r"\b(FROM|JOIN)\s+(\[?)([A-Za-z_][\w@$#]*)(\]?)(?!\s*\.)", re.IGNORECASE)


def _resolve_sql(query, shared, side):
    """Qualify bare table names in agent SQL with the schema they live in on this side.

    The agent writes `FROM ActivityTaskPlan` because that is the name it was shown, but
    the same table is `dbo.ActivityTaskPlan` in one database and `well.activity_task_plan`
    in the other - unqualified, it simply does not resolve. Each side's own schema map is
    applied here, so one piece of SQL can run against both databases.

    Returns (sql, [notes]) - the notes go into the observation so the rewrite is visible.
    """
    schema = (shared.get("schema") or {}).get(side, {})
    if not schema:
        return query, []

    # Both the paired key ("task_daily") and this side's real table name resolve.
    by_name = {}
    for key, entry in schema.items():
        by_name.setdefault(key.lower(), entry)
        by_name.setdefault(entry["table"].lower(), entry)

    notes = []

    def replace(match):
        keyword, open_bracket, name, close_bracket = match.groups()
        entry = by_name.get(name.lower())
        if not entry:
            return match.group(0)
        qualified = f"{db.quote_ident(entry['schema'])}.{db.quote_ident(entry['table'])}"
        if entry["table"].lower() != name.lower() or entry["schema"] != "dbo":
            notes.append(f"{name} -> {entry['schema']}.{entry['table']}")
        return f"{keyword} {qualified}"

    return SQL_OBJECT.sub(replace, query), notes


# Words that are part of T-SQL rather than names of things in the schema. The check below
# only flags what is left after these, so the list errs on the side of being too long: a
# missing keyword means a valid query is refused, which is worse than a bad one running.
SQL_WORDS = {
    "select", "from", "where", "group", "by", "order", "having", "join", "inner", "left",
    "right", "full", "outer", "cross", "apply", "on", "as", "and", "or", "not", "null",
    "is", "in", "like", "between", "distinct", "top", "percent", "case", "when", "then",
    "else", "end", "union", "all", "except", "intersect", "with", "over", "partition",
    "asc", "desc", "exists", "any", "some", "into", "values", "set", "declare", "begin",
    "offset", "fetch", "next", "rows", "only", "nolock", "using", "pivot", "unpivot",
    # functions and types the model reaches for
    "count", "sum", "avg", "min", "max", "abs", "round", "ceiling", "floor", "cast",
    "convert", "try_cast", "try_convert", "isnull", "coalesce", "nullif", "iif", "len",
    "datalength", "trim", "ltrim", "rtrim", "upper", "lower", "substring", "charindex",
    "replace", "concat", "concat_ws", "format", "getdate", "getutcdate", "sysdatetime",
    "dateadd", "datediff", "datepart", "datename", "year", "month", "day", "eomonth",
    "row_number", "rank", "dense_rank", "ntile", "lag", "lead", "first_value",
    "last_value", "stdev", "var", "string_agg", "json_value", "int", "bigint",
    "smallint", "tinyint", "bit", "decimal", "numeric", "float", "real", "money",
    "char", "varchar", "nchar", "nvarchar", "text", "date", "datetime", "datetime2",
    "smalldatetime", "time", "uniqueidentifier", "varbinary", "max",
}

STRING_LITERAL = re.compile(r"'[^']*'")
BRACKETED = re.compile(r"\[([^\]]+)\]")
ALIAS = re.compile(r"\bAS\s+\[?([A-Za-z_][\w@$#]*)\]?", re.IGNORECASE)
TABLE_ALIAS = re.compile(
    r"\b(?:FROM|JOIN)\s+\[?[\w@$#]+\]?(?:\.\[?[\w@$#]+\]?)?\s+(?:AS\s+)?\[?([A-Za-z_][\w@$#]*)\]?",
    re.IGNORECASE,
)
# `WITH c AS (...)` and `, d AS (...)`: a common table expression names something the
# query itself defines, so it is not a column and not a missing table.
CTE = re.compile(r"(?:\bWITH|,)\s*\[?([A-Za-z_][\w@$#]*)\]?\s+AS\s*\(", re.IGNORECASE)
WORD = re.compile(r"[A-Za-z_][\w@$#]*")


def _known_columns(query, shared, side):
    """Every column of every table this query reads on this side, lowercased."""
    schema = (shared.get("schema") or {}).get(side, {})
    by_name = {}
    for key, entry in schema.items():
        by_name.setdefault(key.lower(), entry)
        by_name.setdefault(entry["table"].lower(), entry)

    columns, tables = set(), set()
    for _, _, name, _ in SQL_OBJECT.findall(query):
        entry = by_name.get(name.lower())
        if entry:
            tables.add(name.lower())
            columns |= {c.lower() for c in entry["columns"]}
    return columns, tables


def _unknown_columns(query, shared, side):
    """Names in the query that are not columns of the tables it reads.

    The agent cannot see the columns, so it guesses them - and a wrong guess costs a full
    step for an `Invalid column name` that says nothing about what the columns really are.
    Checking here is cheap and the answer can list the real ones.

    Deliberately conservative: it only runs when the query's tables were recognised, it
    ignores string literals, T-SQL words, aliases the query defines, function calls and
    anything bracket-quoted (the agent quoting a name is taken as meaning it).
    """
    columns, tables = _known_columns(query, shared, side)
    if not tables or not columns:
        return []

    text = STRING_LITERAL.sub("''", query)
    defined = (
        {a.lower() for a in ALIAS.findall(text)}
        | {a.lower() for a in TABLE_ALIAS.findall(text)}
        | {c.lower() for c in CTE.findall(text)}
    )
    quoted = {b.lower() for b in BRACKETED.findall(text)}

    unknown = []
    for match in WORD.finditer(text):
        word = match.group(0)
        lowered = word.lower()
        after = text[match.end():match.end() + 1]
        before = text[max(0, match.start() - 1):match.start()]
        if (
            lowered in SQL_WORDS
            or lowered in columns
            or lowered in tables
            or lowered in defined
            or lowered in quoted
            or after == "("            # a function call
            or before == "["           # bracket-quoted: the agent means it
            or word.isdigit()
        ):
            continue
        if lowered not in (u.lower() for u in unknown):
            unknown.append(word)
    return unknown


# SUM/AVG over text is the mistake a schema drift invites: the column is a number on one
# side and nvarchar on the other, so the same query works against B and fails against A
# after a full scan. MIN/MAX/COUNT are left alone - T-SQL accepts those on text.
NUMERIC_AGGREGATE = re.compile(
    r"\b(SUM|AVG|STDEV|STDEVP|VAR|VARP)\s*\(\s*(?:DISTINCT\s+)?\[?([A-Za-z_][\w@$#]*)\]?\s*\)",
    re.IGNORECASE,
)
NON_NUMERIC_TYPES = (
    "char", "varchar", "nchar", "nvarchar", "text", "ntext", "uniqueidentifier", "xml",
    "binary", "varbinary", "image", "bit", "date", "time", "datetime", "datetime2",
    "smalldatetime", "datetimeoffset",
)


def _type_problems(query, shared, side):
    """Numeric aggregates over a column that is not numeric on this side.

    The type difference is in the diff already, so this both prevents a failing scan and
    tells the agent what the drift is - which is the thing it was trying to find out.
    """
    schema = (shared.get("schema") or {}).get(side, {})
    other = (shared.get("schema") or {}).get("b" if side == "a" else "a", {})
    if not schema:
        return []

    def find(store, column):
        for key, entry in store.items():
            for name, meta in entry["columns"].items():
                if name.lower() == column.lower():
                    return key, meta["type"]
        return None, None

    problems = []
    for function, column in NUMERIC_AGGREGATE.findall(STRING_LITERAL.sub("''", query)):
        table, kind = find(schema, column)
        if not kind or not kind.lower().startswith(NON_NUMERIC_TYPES):
            continue
        _, other_kind = find(other, column)
        drift = (
            f" (it is {other_kind} in the other database - that type change is itself a"
            f" finding)"
            if other_kind and other_kind != kind
            else ""
        )
        problems.append(
            f"{function}({column}) cannot run here: {column} is {kind} in {table}{drift}."
            f" Use TRY_CAST({column} AS float) on both sides, or aggregate a column that"
            f" is numeric in both."
        )
    return problems


WHERE_CLAUSE = re.compile(r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|$)",
                          re.IGNORECASE | re.DOTALL)
# A predicate that can actually exclude rows. `IS NOT NULL` and `<> ''` are WHERE clauses
# that keep everything, which is how a full scan slipped past the first version of this
# guard: the agent added `WHERE schedule_id IS NOT NULL` and still read 6.3 million rows.
COMPARISON = re.compile(
    r"\b([A-Za-z_][\w@$#]*)\s*(?:=|>|<|>=|<=|\bBETWEEN\b|\bIN\s*\()",
    re.IGNORECASE,
)
AGGREGATE_OR_GROUP = re.compile(
    r"\b(GROUP\s+BY|COUNT|SUM|AVG|MIN|MAX|DISTINCT|STDEV|VAR)\b", re.IGNORECASE
)


def _indexed_columns(entry):
    """Leading columns of this table's indexes: the ones a range filter can seek on."""
    leading = []
    for index in entry.get("indexes", []):
        definition = index.get("definition", "")
        if "(" not in definition:
            continue
        columns = definition.split("(", 1)[1].split(")", 1)[0]
        first = columns.split(",")[0].strip().replace(" DESC", "")
        if first and first not in leading:
            leading.append(first)
    return leading + [c for c in entry.get("primary_key", []) if c not in leading]


def _refused(shared, table):
    """Count refusals per table, so a repeated one can say something stronger.

    The guards answer instantly, but an agent that keeps circling the same impossible
    table still burns its budget one step at a time. The second refusal says so plainly.
    """
    seen = shared.setdefault("refusals", {})
    seen[table] = seen.get(table, 0) + 1
    if seen[table] < 2:
        return ""
    return (
        f" This is attempt {seen[table]} on {table}: it cannot tell you more than the"
        f" structural diff already does. Investigate a different table, or finish."
    )


def _narrows(query, entry):
    """True when the query's WHERE can actually exclude rows on an indexed column.

    Any WHERE used to be enough, which an agent satisfies with `WHERE col IS NOT NULL` -
    a clause that reads every row anyway. What matters is a comparison (=, >, <, BETWEEN,
    IN) against a column the server can seek on; anything else is a full scan wearing a
    WHERE clause.
    """
    match = WHERE_CLAUSE.search(query)
    if not match:
        return False
    indexed = {c.lower() for c in _indexed_columns(entry)}
    if not indexed:
        return False
    return any(
        column.lower() in indexed for column, in
        ((m.group(1),) for m in COMPARISON.finditer(match.group(1)))
    )


def _scan_problem(query, shared, side):
    """An unbounded aggregate over a table too big to read inside the timeout.

    `SELECT project_id, COUNT(*) FROM activity_task_plan GROUP BY project_id` has to touch
    all 71 million rows; it timed out at 120s and told the agent nothing. The row counts
    are already known, so this can be said before the query runs, with the fix attached.
    """
    if not AGGREGATE_OR_GROUP.search(query):
        return None

    schema = (shared.get("schema") or {}).get(side, {})
    by_name = {}
    for key, entry in schema.items():
        by_name.setdefault(key.lower(), entry)
        by_name.setdefault(entry["table"].lower(), entry)

    for _, _, name, _ in SQL_OBJECT.findall(query):
        entry = by_name.get(name.lower())
        rows = (entry or {}).get("row_count")
        if rows and rows > config.SQL_SCAN_MAX_ROWS and not _narrows(query, entry):
            # "Add a WHERE" is only useful advice if the filter can use an index: a range
            # over an unindexed column on a table this size is still a full scan, and
            # still times out. So the columns that are actually indexed are named here.
            indexed = _indexed_columns(entry)
            usable = (
                f" Compare an indexed column to a value or range - {', '.join(indexed)}"
                f" - for example WHERE {indexed[0]} = '...' or WHERE {indexed[0]} BETWEEN"
                f" ... AND ...; a range over anything else still scans the table."
                if indexed
                else " Note the table has no index to filter on, so any range still scans"
                " it; a different question may be the only way."
            )
            has_where = bool(WHERE_CLAUSE.search(query))
            why = (
                "its WHERE does not narrow anything a server can seek on (IS NOT NULL"
                " keeps every row), so it still reads all of them"
                if has_where
                else "this query has no WHERE, so it reads all of them"
            )
            return (
                f"{entry['schema']}.{entry['table']} has {rows:,} rows and {why} - that is"
                f" what timed out before.{usable} Pass force: true to try it anyway."
            )
    return None


def _fetch_rows_degrading(dsn, table, schema, pk, limit):
    """Read rows, and on a timeout read fewer rather than giving up.

    A wide table on a busy server can take longer than the statement timeout. Halving the
    limit until it fits turns a dead end into a partial comparison the agent can still
    reason about - and the observation says plainly that it is partial.

    Returns (rows, limit_actually_used).
    """
    attempt = int(limit)
    for last in (False, True):
        try:
            return db.fetch_rows(dsn, table, schema, order_by=pk, limit=attempt), attempt
        except db.QueryTimeout:
            # One retry only: each attempt costs a full statement timeout, so a long
            # ladder turns one slow table into minutes of waiting.
            if last or attempt <= MIN_ROW_LIMIT:
                raise
            attempt = MIN_ROW_LIMIT


def _locate(shared, side, table):
    """Where a paired table really lives on one side: (schema, table).

    The key the agent uses is the paired name; the physical object can sit in a different
    schema on each side, so it is always looked up rather than assumed.
    """
    entry = (shared.get("schema") or {}).get(side, {}).get(table)
    if not entry:
        raise ValueError(f"'{table}' is not a known table in {_label(side)}")
    return entry["schema"], entry["table"]


# T-SQL mistakes a model makes repeatedly. The raw ODBC text says what is wrong but not
# what to do, so the agent retries the same query; the hint is what turns the error into
# a usable observation.
SQL_HINTS = (
    ("incorrect syntax near the keyword",
     "that word is reserved in T-SQL - bracket it, e.g. COUNT(*) AS [RowCount], or pick"
     " another alias"),
    ("invalid column name",
     "that column does not exist on this side - check the diff, or use sample_rows to see"
     " the real column names"),
    ("invalid object name",
     "write the table name bare, exactly as the diff spells it; each side resolves it to"
     " its own schema"),
    ("ambiguous column name",
     "qualify the column with its table alias"),
    ("must appear in the group by",
     "every selected column that is not aggregated has to be in GROUP BY"),
    ("converting data type",
     "that column holds text on this side even though it is numeric on the other - that"
     " type change is itself a finding. Wrap it in TRY_CAST(col AS float) on both sides,"
     " or aggregate a column that is numeric in both databases"),
    ("invalid for sum operator",
     "SUM does not accept that type - cast it, or count the rows instead"),
    ("timed out",
     "narrow the query with a WHERE range on a date or key column, or aggregate a smaller"
     " table - the statement timeout is not going to be enough for a full scan"),
)


def _sql_hint(exc):
    """A short, actionable addition to a SQL error, or "" when there is nothing to add."""
    text = str(exc).lower()
    for needle, hint in SQL_HINTS:
        if needle in text:
            return f"  HINT: {hint}."
    return ""


def execute_tool(tool, params, shared):
    params = params or {}
    schema = config.SCHEMA

    if tool == "compare_schema":
        raw_a = db.fetch_schema(config.dsn_a(), schema, only=shared.get("tables"))
        raw_b = db.fetch_schema(config.dsn_b(), schema, only=shared.get("tables"))
        picked = set(shared.get("tables") or [])
        sa, sb = differ.pair_by_name(
            raw_a, raw_b, chosen_a=picked & set(raw_a), chosen_b=picked & set(raw_b)
        )
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
        table_diff = diff["table_diffs"][table]
        pk = differ.row_key(table_diff)
        if not pk:
            declared = table_diff["primary_key_a"] or table_diff["primary_key_b"]
            why = (
                f"its key {declared} does not exist on both sides"
                if declared
                else "neither side declares a primary key"
            )
            return (
                f"{table} cannot be compared row by row: {why}. Use run_sql with an"
                f" aggregate, or sample_rows to look at its contents."
            ) + _refused(shared, table), {
                "table": table,
                "error": "no usable key",
            }

        counts = [c for c in (table_diff["row_count_a"], table_diff["row_count_b"])
                  if c is not None]
        biggest = max(counts) if counts else 0
        if biggest > config.ROW_COMPARE_MAX_ROWS and not params.get("force"):
            return (
                f"{table} is too large for a row by row comparison: "
                f"{table_diff['row_count_a']} rows in A vs {table_diff['row_count_b']} in B"
                f" (limit {config.ROW_COMPARE_MAX_ROWS}). Reading both sides in full would"
                f" time out. Use run_sql for an aggregate check instead - counts grouped by"
                f" a status, date or key column on each side - or pass force: true to"
                f" compare the first {config.ROW_COMPARE_LIMIT} rows by primary key only."
            ) + _refused(shared, table), {
                "table": table,
                "error": "too large for a row comparison",
                "row_count_a": table_diff["row_count_a"],
                "row_count_b": table_diff["row_count_b"],
            }
        schema_a, name_a = _locate(shared, "a", table)
        schema_b, name_b = _locate(shared, "b", table)
        rows_a, limit_a = _fetch_rows_degrading(
            config.dsn_a(), name_a, schema_a, pk, limit
        )
        rows_b, limit_b = _fetch_rows_degrading(
            config.dsn_b(), name_b, schema_b, pk, limit
        )
        res = differ.diff_rows(rows_a, rows_b, pk)
        res["row_limit"] = min(limit_a, limit_b)
        res["partial"] = res["row_limit"] < limit
        res["table"] = table
        res["object_a"] = f"{schema_a}.{name_a}"
        res["object_b"] = f"{schema_b}.{name_b}"
        shared.setdefault("data_diffs", {})[table] = res

        lines = [
            f"Row comparison of {table} on primary key {pk}"
            f" ({res['object_a']} in A vs {res['object_b']} in B):",
        ]
        if res["partial"]:
            lines.append(
                f"  NOTE the full read timed out; this compares the first"
                f" {res['row_limit']} rows by primary key, not the whole table."
            )
        lines += [
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
        side = "a" if str(which).upper().startswith("A") else "b"
        table_schema, table_name = _locate(shared, side, table)
        rows, _ = _fetch_rows_degrading(
            _dsn_for(which), table_name, table_schema, None, limit
        )
        head = f"{limit} sample rows from {table} in {_label(which)}:"
        # Naming the columns once instead of on every row keeps the whole sample inside
        # the budget that used to truncate each row halfway through.
        body = toon.clip(toon.encode(rows[:limit], name="sample"), SAMPLE_CHARS)
        return f"{head}\n{body}", {"database": which, "table": table, "rows": rows}

    if tool == "run_sql":
        which = str(params.get("database", "both")).lower()
        query = params.get("sql")
        if not query:
            raise ValueError("run_sql needs a 'sql' param")
        targets = ["A", "B"] if which.startswith("bo") else [which.upper()[:1]]
        out, lines = {}, []
        for t in targets:
            side = "a" if t == "A" else "b"
            if not params.get("force"):
                scan = _scan_problem(query, shared, side)
                if scan:
                    table = scan.split(" has ", 1)[0].split(".")[-1]
                    raise ValueError(f"In {_label(t)}: {scan}{_refused(shared, table)}")

            problems = _type_problems(query, shared, side)
            if problems:
                raise ValueError(f"In {_label(t)}: " + " ".join(problems))

            unknown = _unknown_columns(query, shared, side)
            if unknown:
                columns, _ = _known_columns(query, shared, side)
                listed = ", ".join(sorted(columns)[:40])
                more = f" (+{len(columns) - 40} more)" if len(columns) > 40 else ""
                raise ValueError(
                    f"{', '.join(unknown)} is not a column of that table in {_label(t)}."
                    f" Its columns are: {listed}{more}. Use one of those, or sample_rows"
                    f" to see the values."
                )

            resolved, rewrites = _resolve_sql(query, shared, side)
            try:
                rows = db.run_select(
                    _dsn_for(t), resolved, limit=int(params.get("limit") or 200)
                )
            except db.QueryTimeout as exc:
                raise ValueError(
                    f"{exc}{_sql_hint('timed out')}"
                ) from exc
            except Exception as exc:
                raise ValueError(f"{exc}{_sql_hint(exc)}") from exc
            out[t] = rows
            note = f"  [{', '.join(rewrites)}]" if rewrites else ""
            lines.append(f"{_label(t)} ({len(rows)} rows):{note}")
            lines.append(toon.clip(toon.encode(rows[:15], name="result"), RESULT_CHARS))
        return "\n".join(lines), {"sql": query, "results": out}

    raise ValueError(f"Unknown tool: {tool}")
