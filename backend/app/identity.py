"""Identity counters: will the next new row get a number that is already used?

A copy that inserts rows with their original ids (as a migration does) can leave the
table's identity counter behind the highest id. The first new row after go-live then gets
a number that already exists, and the insert fails. This reads the counter and the highest
(and lowest) value of the column in one query, so both describe the same moment.

Used by the ATNM copy check (the RDS copy) and the target table checks (AlTasnimBI).
"""


def _q(name):
    return "[" + name.replace("]", "]]") + "]"


SQL = """
SELECT CAST(ic.seed_value AS decimal(38, 0)) AS seed, CAST(ic.increment_value AS decimal(38, 0)) AS inc,
       CAST(ic.last_value AS decimal(38, 0)) AS last_value,
       (SELECT MAX(x.{col}) FROM {table} x) AS hi, (SELECT MIN(x.{col}) FROM {table} x) AS lo
FROM sys.identity_columns ic
WHERE ic.object_id = OBJECT_ID(?) AND ic.name = ?
"""


def query(schema, table, column):
    """(sql, params) reading one identity column's counter and its highest/lowest value.
    Names come from the live catalog, never from input."""
    full = f"{_q(schema)}.{_q(table)}"
    return SQL.format(col=_q(column), table=full), (full, column)


def _int(v):
    return None if v is None else int(v)


def state(column, row):
    """The counter of `column` from a `query` row: the next value it gives, and `behind`
    when that value (or a later one) is already used."""
    seed, inc, last = _int(row["seed"]), _int(row["inc"]) or 1, _int(row["last_value"])
    hi, lo = _int(row["hi"]), _int(row["lo"])
    nxt = seed if last is None else last + inc
    behind = (hi is not None and hi >= nxt) if inc > 0 else (lo is not None and lo <= nxt)
    return {"column": column, "seed": seed, "increment": inc, "last_value": last, "next_value": nxt,
            "highest": hi, "lowest": lo, "behind": behind}


def text(table, s):
    """The finding for a counter that is behind."""
    used = s["highest"] if s["increment"] > 0 else s["lowest"]
    return (f"The identity counter of {table}.{s['column']} is behind: the next new row would get {s['next_value']:,}, "
            f"but {used:,} is already used, so new inserts will fail until the counter is reset (DBCC CHECKIDENT).")
