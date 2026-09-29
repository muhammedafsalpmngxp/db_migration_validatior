"""Data check: are the values in the target the same as in the source?

Everything runs inside SQL Server as cross-database queries from the target connection
(all three databases live on one server), read uncommitted and read-only. Nothing is
pulled into Python except counts and a few example values.

For one mapping (one_to_one, union or merge):

1. Rows are matched on a key: the target's primary key paired with a source column, or
   another paired id column. A key is used only when it is unique on both sides and its
   values actually overlap - a renumbered id (source 1..n, target 35..) is rejected. With
   no usable key, rows are compared as whole-row fingerprints (a multiset of SHA-256
   hashes), so duplicates count too.
2. Every paired column is compared after converting the source value to the target type
   (text is compared as text, so a truncation shows as a difference). Each matched row
   falls into exactly one bucket per column:
       identical      same value (binary comparison for text)
       case_only      same apart from upper/lower case
       added          source NULL, target has a value
       blank_to_null  source '' (blank), target NULL
       lost           source has a value, target NULL
       recoded        source value does not convert to the target type, target has one
                      ('Active' -> 1, 'Nimr' -> 3522)
       different      both have a value and they differ
3. A recoded column that is a foreign key is translated back through the referenced
   table: the referenced column whose values best match the source text is found, and the
   comparison is repeated on the translated value. Recoding with no foreign key is checked
   for consistency (each source value maps to one target value) and shown for review.
4. NULL and blank counts are read for every column on both sides.

The verdict is `problems` when any count points at lost, missing or changed data,
`review` when values match but something needs a human to confirm it (a recoding rule, a
case change), and `identical` otherwise.
"""
import json
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

import pyodbc

from . import compare, config, db

NOCOMPARE = {"text", "ntext", "image", "xml", "timestamp", "rowversion", "geography",
             "geometry", "hierarchyid", "sql_variant"}
TEXT = {"varchar", "nvarchar", "char", "nchar"}
DATES = {"datetime", "datetime2", "smalldatetime", "date", "time", "datetimeoffset"}
BINARY = {"binary", "varbinary"}
CI = "Latin1_General_CI_AS"      # one collation for every text expression: no conflicts
BIN = "Latin1_General_BIN2"      # exact, case-sensitive comparison
NULL_MARK = "␀"             # stands for NULL inside a fingerprint
BUCKETS = ("identical", "case_only", "added", "blank_to_null", "lost", "recoded", "different")
EXAMPLES = 3


class Skipped(Exception):
    """The mapping cannot be checked right now; the message says why."""


def _q(name):
    return "[" + name.replace("]", "]]") + "]"


def _full(side, schema, table):
    return f"{_q(config.DATABASES[side]['name'])}.{_q(schema)}.{_q(table)}"


def _bt(type_name):
    return type_name.split("(")[0].lower()


def _run(sql, params=()):
    """Rows of one read-only query, from the target connection. User input only ever
    arrives through `params`, never inside `sql`."""
    with closing(db.connect(config.TARGET_SIDE)) as con:
        con.timeout = config.DATA_CHECK_TIMEOUT
        cur = con.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
        except pyodbc.Error as exc:
            if db.is_lock_timeout(exc):
                raise Skipped("A table is locked by another session (a load in progress?).") from exc
            raise
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def _one(sql):
    return _run(sql)[0]


def _n(v):
    return int(v or 0)


# ---- expressions ------------------------------------------------------------------------

def _raw_text(expr, type_name):
    """A value as trimmed text, for display, blank tests and lookup matching."""
    bt = _bt(type_name)
    style = ", 121" if bt in DATES else ", 1" if bt in BINARY else ""
    return f"LTRIM(RTRIM(CONVERT(nvarchar(4000), {expr}{style}))) COLLATE {CI}"


class Pair:
    """One source column paired with one target column, and how to compare them."""

    def __init__(self, i, row):
        self.i = i
        self.source = row["source"]
        self.target = row["target"]
        self.match = row["match"]
        self.lookup = None       # {"table", "schema", "column", "key", "coverage"} when translated

    @property
    def stype(self):
        return self.source["type"]

    @property
    def ttype(self):
        return self.target["type"]

    @property
    def text_compare(self):
        return self.lookup is not None or _bt(self.ttype) in TEXT

    def mode(self):
        if self.lookup:
            return "lookup"
        return "direct" if self.stype.lower() == self.ttype.lower() else "converted"

    def source_value(self, col_expr, col_type):
        """The source value in the target's terms (NULL when it does not convert)."""
        if self.lookup:
            return _raw_text(col_expr, col_type)
        if _bt(self.ttype) in TEXT:
            style = ", 121" if _bt(col_type) in DATES else ""
            return f"CONVERT(nvarchar(max), {col_expr}{style}) COLLATE {CI}"
        if col_type.lower() == self.ttype.lower():
            return col_expr
        return f"TRY_CONVERT({self.ttype}, {col_expr})"

    def target_value(self, alias="y", join_alias=None):
        col = f"{alias}.{_q(self.target['name'])}"
        if self.lookup:
            ref = f"{join_alias}.{_q(self.lookup['column'])}"
            return (f"CASE WHEN {col} IS NULL THEN NULL ELSE ISNULL({_raw_text(ref, self.lookup['type'])}, "
                    f"N'(no row in {self.lookup['schema']}.{self.lookup['table']})') END")
        if _bt(self.ttype) in TEXT:
            return f"CONVERT(nvarchar(max), {col}) COLLATE {CI}"
        return col


def _fingerprint_part(expr, pair, collation=BIN):
    style = ", 126" if not pair.text_compare and _bt(pair.ttype) in DATES else ""
    return f"ISNULL(CONVERT(nvarchar(max), {expr}{style}) COLLATE {collation}, N'{NULL_MARK}')"


# ---- the source relation: one or more tables stacked --------------------------------------

class Side:
    """The source tables of a mapping as one relation, and the target table."""

    def __init__(self, sources, target, pairs):
        self.sources = sources   # [(member, entry, {lower name: column})]
        self.target = target     # (member, entry, [columns])
        self.pairs = pairs

    def source_sql(self, key_pair=None):
        """SELECT per source table, UNION ALL-ed: __src, __k, c<i> (target-typed), r<i> (text)."""
        branches = []
        for member, entry, cols in self.sources:
            parts = [f"N'{entry['table']}' AS __src", "1 AS __s"]
            if key_pair:
                col = cols.get(key_pair.source["name"].lower())
                parts.append(f"{key_pair.source_value('x.' + _q(col['name']), col['type'])} AS __k" if col
                             else "CAST(NULL AS int) AS __k")
            for p in self.pairs:
                col = cols.get(p.source["name"].lower())
                if col:
                    expr = "x." + _q(col["name"])
                    parts.append(f"{p.source_value(expr, col['type'])} AS c{p.i}")
                    parts.append(f"{_raw_text(expr, col['type'])} AS r{p.i}")
                else:
                    parts.append(f"CAST(NULL AS {'nvarchar(max)' if p.text_compare else p.ttype}) AS c{p.i}")
                    parts.append(f"CAST(NULL AS nvarchar(4000)) AS r{p.i}")
            branches.append(f"SELECT {', '.join(parts)} FROM {_full(member.ref.side, entry['schema'], entry['table'])} x")
        return "(" + " UNION ALL ".join(branches) + ")"

    def target_sql(self, key_pair=None, raw=False):
        member, entry, _ = self.target
        parts, joins = ["1 AS __t"], []
        if key_pair:
            parts.append(f"{key_pair.target_value()} AS __k")
        for p in self.pairs:
            ja = None
            if p.lookup:
                ja = f"j{p.i}"
                joins.append(f"LEFT JOIN {_full(config.TARGET_SIDE, p.lookup['schema'], p.lookup['table'])} {ja} "
                             f"ON {ja}.{_q(p.lookup['key'])} = y.{_q(p.target['name'])}")
            parts.append(f"{p.target_value('y', ja)} AS t{p.i}")
            # u<i>: what the UI shows as the target value - the translated text for a lookup.
            parts.append(f"{p.target_value('y', ja)} AS u{p.i}" if p.lookup
                         else f"{_raw_text('y.' + _q(p.target['name']), p.ttype)} AS u{p.i}")
            if raw:
                parts.append(f"{_raw_text('y.' + _q(p.target['name']), p.ttype)} AS v{p.i}")
        return (f"(SELECT {', '.join(parts)} FROM "
                f"{_full(config.TARGET_SIDE, entry['schema'], entry['table'])} y {' '.join(joins)})")


# ---- profiles ---------------------------------------------------------------------------------

def _null_profile(side, entry, cols):
    parts = ["COUNT_BIG(*) AS __rows"]
    for i, c in enumerate(cols):
        parts.append(f"SUM(CASE WHEN {_q(c['name'])} IS NULL THEN 1 ELSE 0 END) AS n{i}")
        if _bt(c["type"]) in TEXT:
            parts.append(f"SUM(CASE WHEN LTRIM(RTRIM({_q(c['name'])})) = N'' THEN 1 ELSE 0 END) AS b{i}")
    r = _one(f"SELECT {', '.join(parts)} FROM {_full(side, entry['schema'], entry['table'])}")
    return _n(r["__rows"]), {c["name"].lower(): {"nulls": _n(r[f"n{i}"]), "blanks": _n(r.get(f"b{i}"))}
                             for i, c in enumerate(cols)}


def _conversion_failures(side, pairs):
    """Per pair: source values that are filled in but do not convert to the target type."""
    tests = [p for p in pairs if not p.text_compare and p.stype.lower() != p.ttype.lower()]
    if not tests:
        return {}
    src = side.source_sql()
    parts = [f"SUM(CASE WHEN r{p.i} IS NOT NULL AND r{p.i} <> N'' AND c{p.i} IS NULL THEN 1 ELSE 0 END) AS f{p.i}"
             for p in tests]
    r = _one(f"SELECT {', '.join(parts)} FROM {src} s")
    return {p.i: _n(r[f"f{p.i}"]) for p in tests}


def _find_lookup(side, pair, fk):
    """The column of the referenced table whose values best match the source text."""
    ref_cols = db.table_columns(config.TARGET_SIDE, fk["referenced_object_id"])
    cands = [c for c in ref_cols if _bt(c["type"]) not in NOCOMPARE and c["name"] != fk["ref_column"]]
    if not cands:
        return None
    ref = _full(config.TARGET_SIDE, fk["referenced"]["schema"], fk["referenced"]["table"])
    src = side.source_sql()
    subs = [f"(SELECT COUNT_BIG(*) FROM v WHERE v.v IN (SELECT {_raw_text('r.' + _q(c['name']), c['type'])} "
            f"FROM {ref} r)) AS m{k}" for k, c in enumerate(cands)]
    r = _one(f"""WITH v AS (SELECT DISTINCT r{pair.i} AS v FROM {src} s WHERE r{pair.i} IS NOT NULL AND r{pair.i} <> N'')
                 SELECT (SELECT COUNT_BIG(*) FROM v) AS total, {', '.join(subs)}""")
    total = _n(r["total"])
    if not total:
        return None
    best = max(range(len(cands)), key=lambda k: _n(r[f"m{k}"]))
    hits = _n(r[f"m{best}"])
    if hits == 0 or hits < 0.5 * total:
        return None
    return {"schema": fk["referenced"]["schema"], "table": fk["referenced"]["table"],
            "column": cands[best]["name"], "type": cands[best]["type"], "key": fk["ref_column"],
            "coverage": round(hits / total, 4), "distinct_values": total, "distinct_found": hits}


# ---- key choice ---------------------------------------------------------------------------------

def _pick_key(side, pairs, target_pk, fk_cols):
    """A paired column that identifies a row on both sides, or (None, notes)."""
    def looks_like_id(p):
        name = p.target["name"].lower()
        return (p.target["pk"] or p.target["identity"] or name in ("id", "row_id", "uid")
                or name.endswith("_id") or name.endswith("uid"))

    # The target's primary key first, then other id-like columns. A foreign key column
    # points at another table's rows, so it never identifies this table's rows.
    ordered = [p for p in pairs if target_pk and [p.target["name"].lower()] == target_pk]
    ordered += [p for p in pairs if p not in ordered and looks_like_id(p)
                and p.target["name"].lower() not in fk_cols]
    notes = []
    tgt = side.target_sql()
    for p in ordered[:6]:
        src = side.source_sql(key_pair=None)
        r = _one(f"""SELECT
              (SELECT COUNT_BIG(*) FROM {src} s) sn,
              (SELECT COUNT_BIG(c{p.i}) FROM {src} s) snn,
              (SELECT COUNT_BIG(DISTINCT c{p.i}) FROM {src} s) sd,
              (SELECT COUNT_BIG(*) FROM {tgt} t) tn,
              (SELECT COUNT_BIG(t{p.i}) FROM {tgt} t) tnn,
              (SELECT COUNT_BIG(DISTINCT t{p.i}) FROM {tgt} t) td""")
        sn, snn, sd, tn, tnn, td = (_n(r[k]) for k in ("sn", "snn", "sd", "tn", "tnn", "td"))
        label = f"{p.source['name']} → {p.target['name']}"
        if not sn or not tn:
            continue
        if sd != snn or td != tnn:
            notes.append(f"{label} is not unique ({snn - sd:,} duplicate values in source, {tnn - td:,} in target).")
            continue
        if snn < 0.5 * sn or tnn < 0.5 * tn:
            notes.append(f"{label} is mostly NULL, so it cannot identify rows.")
            continue
        overlap = _n(_one(f"SELECT COUNT_BIG(*) AS n FROM {src} s JOIN {tgt} t ON s.c{p.i} = t.t{p.i}")["n"])
        if overlap < 0.5 * min(snn, tnn):
            notes.append(f"{label} is unique on both sides, but only {overlap:,} values are shared: "
                         "the ids were renumbered.")
            continue
        return p, notes
    return None, notes


# ---- keyed comparison ------------------------------------------------------------------------

def _bucket_expr(p):
    s_c, t_c = f"s.c{p.i}", f"t.t{p.i}"
    same = (f"{s_c} COLLATE {BIN} = {t_c} COLLATE {BIN}" if p.text_compare else f"{s_c} = {t_c}")
    lines = [
        "WHEN s.__s IS NULL OR t.__t IS NULL THEN NULL",
        f"WHEN s.r{p.i} IS NULL AND {t_c} IS NULL THEN 0",
        f"WHEN {s_c} IS NOT NULL AND {t_c} IS NOT NULL AND {same} THEN 0",
    ]
    if p.text_compare:
        lines.append(f"WHEN {s_c} IS NOT NULL AND {t_c} IS NOT NULL AND {s_c} COLLATE {CI} = {t_c} COLLATE {CI} THEN 1")
    lines += [
        f"WHEN s.r{p.i} IS NULL THEN 2",
        f"WHEN {t_c} IS NULL AND s.r{p.i} = N'' THEN 3",
        f"WHEN {t_c} IS NULL THEN 4",
        f"WHEN {s_c} IS NULL THEN 5",
        "ELSE 6",
    ]
    return "CASE " + " ".join(lines) + " END"


def _keyed(side, key):
    src, tgt = side.source_sql(key_pair=key), side.target_sql(key_pair=key)
    buckets = ", ".join(f"{_bucket_expr(p)} AS g{p.i}" for p in side.pairs)
    base = f"""WITH s AS {src}, t AS {tgt},
               m AS (SELECT s.__src, s.__s AS sp, t.__t AS tp, s.__k AS sk, t.__k AS tk, {', '.join(f's.r{p.i}, t.u{p.i}' for p in side.pairs)}
                            {', ' + buckets if buckets else ''}
                     FROM s FULL OUTER JOIN t ON s.__k = t.__k)"""
    # sp / tp say which side a joined row came from. A NULL key never matches, so a source
    # row with a NULL key counts as missing and a target row with one as extra.
    sums = [
        "SUM(CASE WHEN sp = 1 THEN 1 ELSE 0 END) AS source_rows",
        "SUM(CASE WHEN tp = 1 THEN 1 ELSE 0 END) AS target_rows",
        "SUM(CASE WHEN sp = 1 AND tp = 1 THEN 1 ELSE 0 END) AS matched",
        "SUM(CASE WHEN sp = 1 AND tp IS NULL THEN 1 ELSE 0 END) AS missing",
        "SUM(CASE WHEN sp = 1 AND tp IS NULL AND sk IS NULL THEN 1 ELSE 0 END) AS missing_null_key",
        "SUM(CASE WHEN tp = 1 AND sp IS NULL THEN 1 ELSE 0 END) AS extra",
        "SUM(CASE WHEN tp = 1 AND sp IS NULL AND tk IS NULL THEN 1 ELSE 0 END) AS extra_null_key",
    ]
    if side.pairs:
        total = " + ".join(f"g{p.i}" for p in side.pairs)
        sums.append(f"SUM(CASE WHEN sp = 1 AND tp = 1 AND ({total}) = 0 THEN 1 ELSE 0 END) AS identical_rows")
        bad = " + ".join(f"CASE WHEN g{p.i} IN (4, 6) THEN 1 ELSE 0 END" for p in side.pairs)
        sums.append(f"SUM(CASE WHEN sp = 1 AND tp = 1 AND ({bad}) > 0 THEN 1 ELSE 0 END) AS problem_rows")
    for p in side.pairs:
        for code, name in enumerate(BUCKETS):
            sums.append(f"SUM(CASE WHEN g{p.i} = {code} THEN 1 ELSE 0 END) AS b{p.i}_{code}")
    r = _one(f"{base} SELECT {', '.join(sums)} FROM m")
    rows = {
        "source": _n(r["source_rows"]),
        "target": _n(r["target_rows"]),
        "matched": _n(r["matched"]),
        "missing_in_target": _n(r["missing"]),
        "extra_in_target": _n(r["extra"]),
        "source_null_keys": _n(r["missing_null_key"]),
        "target_null_keys": _n(r["extra_null_key"]),
        "identical_rows": _n(r.get("identical_rows")) if side.pairs else _n(r["matched"]),
        # matched rows with at least one lost or different value
        "problem_rows": _n(r.get("problem_rows")),
    }
    missing_by_source = []
    missing_examples, extra_examples = [], []
    if rows["missing_in_target"]:
        missing_by_source = [{"table": x["__src"], "rows": _n(x["n"])} for x in _run(
            f"{base} SELECT __src, COUNT_BIG(*) AS n FROM m WHERE sp = 1 AND tp IS NULL GROUP BY __src ORDER BY n DESC")]
        missing_examples = [{"table": x["__src"], "key": x["k"]} for x in _run(
            f"{base} SELECT TOP 10 __src, ISNULL(CONVERT(nvarchar(200), sk), N'NULL') AS k FROM m WHERE sp = 1 AND tp IS NULL ORDER BY sk")]
    if rows["extra_in_target"] - rows["target_null_keys"] > 0:
        extra_examples = [x["k"] for x in _run(
            f"{base} SELECT TOP 10 CONVERT(nvarchar(200), tk) AS k FROM m WHERE tp = 1 AND sp IS NULL AND tk IS NOT NULL ORDER BY tk")]

    columns = {}
    for p in side.pairs:
        b = {name: _n(r[f"b{p.i}_{code}"]) for code, name in enumerate(BUCKETS)}
        col = {"buckets": b, "examples": {}}
        for name in ("different", "lost", "added", "case_only"):
            code = BUCKETS.index(name)
            if b[name]:
                col["examples"][name] = [
                    {"key": x["k"], "source": x["s"], "target": x["t"]} for x in _run(
                        f"{base} SELECT TOP {EXAMPLES} CONVERT(nvarchar(200), sk) AS k, LEFT(r{p.i}, 120) AS s, "
                        f"LEFT(u{p.i}, 120) AS t FROM m WHERE g{p.i} = {code} ORDER BY sk")]
        if b["recoded"]:
            code = BUCKETS.index("recoded")
            col["recoding"] = {
                "pairs": [{"source": x["s"], "target": x["t"], "rows": _n(x["n"])} for x in _run(
                    f"{base} SELECT TOP 8 LEFT(r{p.i}, 80) AS s, LEFT(u{p.i}, 80) AS t, COUNT_BIG(*) AS n "
                    f"FROM m WHERE g{p.i} = {code} GROUP BY r{p.i}, u{p.i} ORDER BY n DESC")],
                "distinct_source_values": _n(_one(
                    f"{base} SELECT COUNT(DISTINCT r{p.i}) AS n FROM m WHERE g{p.i} = {code}")["n"]),
                "inconsistent": _n(_one(
                    f"{base} SELECT COUNT(*) AS n FROM (SELECT r{p.i} FROM m WHERE g{p.i} = {code} "
                    f"GROUP BY r{p.i} HAVING COUNT(DISTINCT u{p.i}) > 1) z")["n"]),
            }
        columns[p.i] = col
    return rows, columns, missing_by_source, missing_examples, extra_examples


# ---- keyless comparison ------------------------------------------------------------------------

def _fingerprint(side, pairs=None):
    """Whole-row comparison as a multiset of hashes over `pairs` (default: every pair)."""
    pairs = side.pairs if pairs is None else pairs
    src, tgt = side.source_sql(), side.target_sql()
    if not pairs:
        raise Skipped("No column can be compared.")
    hs = " + N'|' + ".join(_fingerprint_part(f"x.c{p.i}", p) for p in pairs)
    ht = " + N'|' + ".join(_fingerprint_part(f"y.t{p.i}", p) for p in pairs)
    r = _one(f"""
      WITH s AS (SELECT HASHBYTES('SHA2_256', {hs}) h, COUNT_BIG(*) n FROM {src} x GROUP BY HASHBYTES('SHA2_256', {hs})),
           t AS (SELECT HASHBYTES('SHA2_256', {ht}) h, COUNT_BIG(*) n FROM {tgt} y GROUP BY HASHBYTES('SHA2_256', {ht}))
      SELECT SUM(ISNULL(s.n, 0)) AS source_rows, SUM(ISNULL(t.n, 0)) AS target_rows,
             SUM(CASE WHEN ISNULL(s.n, 0) < ISNULL(t.n, 0) THEN ISNULL(s.n, 0) ELSE ISNULL(t.n, 0) END) AS identical,
             SUM(CASE WHEN ISNULL(s.n, 0) > ISNULL(t.n, 0) THEN s.n - ISNULL(t.n, 0) ELSE 0 END) AS only_s,
             SUM(CASE WHEN ISNULL(t.n, 0) > ISNULL(s.n, 0) THEN t.n - ISNULL(s.n, 0) ELSE 0 END) AS only_t
      FROM s FULL OUTER JOIN t ON s.h = t.h""")
    return {"source": _n(r["source_rows"]), "target": _n(r["target_rows"]), "identical_rows": _n(r["identical"]),
            "only_in_source": _n(r["only_s"]), "only_in_target": _n(r["only_t"])}


def _column_multiset(side, p):
    """How many values of one column have no equal value on the other side."""
    src, tgt = side.source_sql(), side.target_sql()
    fs, ft = _fingerprint_part(f"x.c{p.i}", p), _fingerprint_part(f"y.t{p.i}", p)
    base = f"""WITH s AS (SELECT {fs} v, COUNT_BIG(*) n, MIN(x.r{p.i}) d FROM {src} x GROUP BY {fs}),
                    t AS (SELECT {ft} v, COUNT_BIG(*) n, MIN(y.u{p.i}) d FROM {tgt} y GROUP BY {ft})"""
    r = _one(f"""{base} SELECT
        SUM(CASE WHEN ISNULL(s.n, 0) > ISNULL(t.n, 0) THEN s.n - ISNULL(t.n, 0) ELSE 0 END) AS only_s,
        SUM(CASE WHEN ISNULL(t.n, 0) > ISNULL(s.n, 0) THEN t.n - ISNULL(s.n, 0) ELSE 0 END) AS only_t
        FROM s FULL OUTER JOIN t ON s.v = t.v""")
    out = {"only_in_source": _n(r["only_s"]), "only_in_target": _n(r["only_t"]), "examples": {}}
    if out["only_in_source"] and p.text_compare:
        # The same comparison ignoring upper/lower case: tells a case change from a real one.
        fs_ci, ft_ci = _fingerprint_part(f"x.c{p.i}", p, CI), _fingerprint_part(f"y.t{p.i}", p, CI)
        ci = _one(f"""WITH s AS (SELECT {fs_ci} v, COUNT_BIG(*) n FROM {src} x GROUP BY {fs_ci}),
                           t AS (SELECT {ft_ci} v, COUNT_BIG(*) n FROM {tgt} y GROUP BY {ft_ci})
            SELECT SUM(CASE WHEN ISNULL(s.n, 0) > ISNULL(t.n, 0) THEN s.n - ISNULL(t.n, 0) ELSE 0 END) AS only_s
            FROM s FULL OUTER JOIN t ON s.v = t.v""")
        out["only_in_source_ignoring_case"] = _n(ci["only_s"])
    if out["only_in_source"] or out["only_in_target"]:
        # TOP ... ORDER BY has to sit in a derived table to be UNION-ed.
        rows = _run(f"""{base}
            SELECT * FROM (SELECT TOP {EXAMPLES} 'source' AS side, LEFT(s.d, 120) AS v, s.n - ISNULL(t.n, 0) AS n
                           FROM s LEFT JOIN t ON s.v = t.v
                           WHERE s.n > ISNULL(t.n, 0) ORDER BY s.n - ISNULL(t.n, 0) DESC) a
            UNION ALL
            SELECT * FROM (SELECT TOP {EXAMPLES} 'target' AS side, LEFT(t.d, 120) AS v, t.n - ISNULL(s.n, 0) AS n
                           FROM t LEFT JOIN s ON s.v = t.v
                           WHERE t.n > ISNULL(s.n, 0) ORDER BY t.n - ISNULL(s.n, 0) DESC) b""")
        out["examples"] = {
            "source": [{"value": x["v"], "rows": _n(x["n"])} for x in rows if x["side"] == "source"],
            "target": [{"value": x["v"], "rows": _n(x["n"])} for x in rows if x["side"] == "target"],
        }
    return out


# ---- findings ----------------------------------------------------------------------------------

def _fmt(n):
    return f"{n:,}"


def _column_label(p):
    s, t = p.source["name"], p.target["name"]
    return s if s == t else f"{s} → {t}"


def _examples_text(examples, limit=2):
    return "; ".join(f"{e.get('key', '')}: {e['source'] if e['source'] is not None else 'NULL'} → "
                     f"{e['target'] if e['target'] is not None else 'NULL'}" for e in examples[:limit])


def _diff_hint(m, c):
    """A plain reason for a text difference, when the examples show one."""
    src = [x["value"] for x in m["examples"].get("source", []) if x["value"]]
    tgt = [x["value"] for x in m["examples"].get("target", []) if x["value"]]
    if any(len(a) > len(b) and a.startswith(b) for a in src for b in tgt):
        return f" The target values look truncated (target type {c['ttype']})."
    if any("?" in b for b in tgt) and any(any(ord(ch) > 127 for ch in a) for a in src):
        return " Characters outside the target's code page were replaced by '?'."
    return ""


def _column_verdict(c):
    """(verdict, [findings]) of one column result."""
    label, fs = c["label"], []
    b = c.get("buckets")
    if b is not None:
        if b["lost"]:
            hint = ""
            if c.get("lookup_hint"):
                h = c["lookup_hint"]
                hint = (f" The source values exist in {h['schema']}.{h['table']}.{h['column']}, "
                        "so the lookup was not filled in.")
            fs.append(("error", f"{label}: {_fmt(b['lost'])} values lost — the source has a value, the target is NULL.{hint}"))
        if b["different"]:
            what = "translated value differs" if c["mode"] == "lookup" else "value differs"
            fs.append(("error", f"{label}: {_fmt(b['different'])} rows where the {what} "
                                f"({_examples_text(c['examples'].get('different', []))})."))
        rec = c.get("recoding")
        if rec and rec["inconsistent"]:
            fs.append(("error", f"{label}: {_fmt(rec['inconsistent'])} source values are recoded to more than one "
                                "target value."))
        elif rec:
            sample = ", ".join(f"'{x['source']}' → {x['target']}" for x in rec["pairs"][:3])
            fs.append(("review", f"{label}: recoded consistently ({_fmt(rec['distinct_source_values'])} source values, "
                                 f"e.g. {sample}); confirm this is the intended rule."))
        if b["case_only"]:
            fs.append(("review", f"{label}: {_fmt(b['case_only'])} values differ only in upper/lower case."))
        if b["added"]:
            fs.append(("review", f"{label}: {_fmt(b['added'])} rows have a value in the target where the source is NULL."))
        if b["blank_to_null"]:
            fs.append(("info", f"{label}: {_fmt(b['blank_to_null'])} blank strings became NULL."))
    else:
        # No key: values are compared as multisets, so a row-level claim cannot be made.
        m = c["multiset"]
        lost = c["nulls"]["added"]
        src_ex = ", ".join("NULL" if x["value"] is None else f"'{x['value']}'"
                           for x in m["examples"].get("source", [])[:2])
        tgt_ex = ", ".join("NULL" if x["value"] is None else f"{x['value']}"
                           for x in m["examples"].get("target", [])[:2])
        lk = c.get("lookup") or c.get("lookup_hint")
        if lost:
            where = ""
            if lk:
                where = (f" Translated through {lk['schema']}.{lk['table']}.{lk['column']}: "
                         f"{lk['coverage'] * 100:.1f}% of the distinct source values exist there.")
            fs.append(("error", f"{label}: {_fmt(lost)} values lost — the target has {_fmt(lost)} more NULLs than the "
                                f"source{f' (source values like {src_ex})' if src_ex else ''}.{where}"))
        differing = m["only_in_source"] - lost
        # Values the target has where the source is empty (e.g. ids generated on load).
        filled = (max(0, c["nulls"]["source"] + c["nulls"]["source_blanks"] - c["nulls"]["target"])
                  if c.get("rows_equal") else 0)
        if differing > 0:
            if m.get("only_in_source_ignoring_case") is not None and m["only_in_source_ignoring_case"] - lost <= 0:
                c["soft"] = "case"
                fs.append(("review", f"{label}: {_fmt(differing)} values differ only in upper/lower case "
                                     f"(source {src_ex}; target {tgt_ex})."))
            elif not c["lookup"] and c["cannot_convert"] >= differing:
                c["soft"] = "recoded"
                c["recoded"] = True
                fs.append(("review", f"{label}: recoded — {_fmt(c['cannot_convert'])} source values do not convert "
                                     f"to {c['ttype']} (e.g. {src_ex or 'text'}) and the target stores "
                                     f"{tgt_ex or 'other values'}; confirm the rule."))
            elif filled >= differing:
                c["soft"] = "filled"
                fs.append(("review", f"{label}: the source is empty on {_fmt(filled)} rows and the target has a value "
                                     f"(e.g. {tgt_ex}) — generated during the load?"))
            else:
                fs.append(("error", f"{label}: {_fmt(differing)} source values have no equal value in the target"
                                    f"{f' (e.g. {src_ex})' if src_ex else ''}"
                                    f"{f'; target has e.g. {tgt_ex}' if tgt_ex else ''}.{_diff_hint(m, c)}"))
    if c["mode"] == "lookup" and not any(sev == "error" for sev, _ in fs):
        lk = c["lookup"]
        fs.append(("info", f"{label}: verified through {lk['schema']}.{lk['table']}.{lk['column']} "
                           f"(the id is translated back to the source text)."))
    if any(s == "error" for s, _ in fs):
        verdict = "problem"
    elif any(s == "review" for s, _ in fs):
        verdict = "review"
    else:
        verdict = "identical"
    return verdict, fs


# ---- one mapping -------------------------------------------------------------------------------

def check(m, entry_of):
    """Data check of one mapping. `entry_of(member)` returns its live table entry."""
    started = time.time()
    result = {"mapping": m.id, "type": m.type, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        result.update(_check(m, entry_of))
    except Skipped as exc:
        result.update(status="skipped", headline=str(exc), findings=[{"severity": "info", "text": str(exc)}])
    result["seconds"] = round(time.time() - started, 1)
    return result


class Prepared:
    """The tables of a mapping, their columns and pairs, and the SQL for both sides."""


def prepare(m, entry_of):
    """Everything a comparison of mapping `m` needs; raises Skipped when it cannot run.

    Shared by the data check and the values view, so both pair and convert columns the
    same way.
    """
    if m.type == "excluded":
        raise Skipped("Excluded from the migration: nothing to compare.")
    if m.type == "transform":
        raise Skipped("Transform mapping: business logic reshapes the rows, so they cannot be compared one to one.")

    tmember = m.targets[0]
    tentry = entry_of(tmember)
    srcs = m.sources if m.type == "union" else [next((s for s in m.sources if s.role == "driving"), m.sources[0])]
    sentries = [entry_of(s) for s in srcs]
    for mem, e in [(tmember, tentry)] + list(zip(srcs, sentries)):
        if e is None:
            raise Skipped(f"{mem.ref.ref} does not exist.")
        if e["locked"]:
            raise Skipped(f"{mem.ref.ref} is locked by another session (a load in progress?). Try again later.")
    total_rows = tentry["rows"] + sum(e["rows"] for e in sentries)
    if total_rows > config.DATA_CHECK_MAX_ROWS:
        raise Skipped(f"{_fmt(total_rows)} rows in this mapping, over the {_fmt(config.DATA_CHECK_MAX_ROWS)} "
                      "row limit for a data check (DATA_CHECK_MAX_ROWS).")

    tcols = db.table_columns(config.TARGET_SIDE, tentry["object_id"])
    source_cols = [db.table_columns(s.ref.side, e["object_id"]) for s, e in zip(srcs, sentries)]
    keys = db.table_keys(config.TARGET_SIDE, tentry["object_id"])
    outgoing = {f["columns"][0].lower(): f for f in keys["foreign_keys"]
                if f["direction"] == "outgoing" and len(f["columns"]) == 1}
    fk_cols = set(outgoing)
    pk = next((k for k in keys["keys"] if k["kind"] == "primary"), None)

    rows, _ = compare.align_columns(source_cols[0], tcols, target_table=tentry["table"],
                                    declared=m.columns, fk_columns=fk_cols)
    comparable = [r for r in rows if r["source"] and r["target"]
                  and _bt(r["source"]["type"]) not in NOCOMPARE and _bt(r["target"]["type"]) not in NOCOMPARE]
    pairs = [Pair(i, r) for i, r in enumerate(comparable)]

    p = Prepared()
    p.srcs, p.sentries, p.tmember, p.tentry = srcs, sentries, tmember, tentry
    p.tcols, p.source_cols, p.keys, p.outgoing, p.fk_cols, p.pk = tcols, source_cols, keys, outgoing, fk_cols, pk
    p.rows, p.comparable, p.pairs = rows, comparable, pairs
    p.not_compared = [f"{r['source']['name']} ({r['source']['type']})" for r in rows
                      if r["source"] and r["target"] and r not in comparable]
    p.side = Side([(s, e, {c["name"].lower(): c for c in cols}) for s, e, cols in zip(srcs, sentries, source_cols)],
                  (tmember, tentry, tcols), pairs)
    return p


def _check(m, entry_of):
    prep = prepare(m, entry_of)
    srcs, sentries, tmember, tentry = prep.srcs, prep.sentries, prep.tmember, prep.tentry
    tcols, source_cols, keys, outgoing = prep.tcols, prep.source_cols, prep.keys, prep.outgoing
    fk_cols, pk, rows, comparable, pairs = prep.fk_cols, prep.pk, prep.rows, prep.comparable, prep.pairs
    not_compared, side = prep.not_compared, prep.side

    # NULL profiles of every column, source tables summed.
    target_rows, tnulls = _null_profile(config.TARGET_SIDE, tentry, tcols)
    snulls, source_rows = {}, 0
    for s, e, cols in zip(srcs, sentries, source_cols):
        n, prof = _null_profile(s.ref.side, e, cols)
        source_rows += n
        for k, v in prof.items():
            acc = snulls.setdefault(k, {"nulls": 0, "blanks": 0})
            acc["nulls"] += v["nulls"]
            acc["blanks"] += v["blanks"]

    # Foreign key columns whose source values do not convert: translate through the lookup.
    failures = _conversion_failures(side, pairs)
    lookup_hints = {}
    for p in pairs:
        fk = outgoing.get(p.target["name"].lower())
        if not fk or _bt(p.stype) not in TEXT or not failures.get(p.i):
            continue
        ref_entry = db.query(config.TARGET_SIDE, "SELECT OBJECT_ID(?) AS o",
                             (f"{_q(fk['referenced']['schema'])}.{_q(fk['referenced']['table'])}",))[0]["o"]
        if not ref_entry:
            continue
        found = _find_lookup(side, p, {**fk, "referenced_object_id": ref_entry, "ref_column": fk["ref_columns"][0]})
        if found:
            p.lookup = found
            lookup_hints[p.i] = found

    result = {"source": " + ".join(f"{e['schema']}.{e['table']}" for e in sentries),
              "target": f"{tentry['schema']}.{tentry['table']}",
              "key_notes": []}
    if source_rows == 0 and target_rows == 0:
        result.update(status="identical", headline="Both sides are empty.", method=None, key=None,
                      rows={"source": 0, "target": 0}, columns=[], findings=[
                          {"severity": "info", "text": "0 rows in the source and in the target."}])
        return result

    key, notes = _pick_key(side, [p for p in pairs if not p.lookup], [c.lower() for c in pk["columns"]] if pk else None,
                           fk_cols)
    result["key_notes"] = notes
    col_results = {}
    if key:
        result["method"] = "key"
        result["key"] = {"source": key.source["name"], "target": key.target["name"]}
        rows_info, cols, by_src, miss_ex, extra_ex = _keyed(side, key)
        result["rows"] = rows_info
        result["missing_by_source"] = by_src
        result["missing_examples"] = miss_ex
        result["extra_examples"] = extra_ex
        col_results = cols
    else:
        result["method"] = "fingerprint"
        result["key"] = None
        result["rows"] = _fingerprint(side)
        mismatch = result["rows"]["only_in_source"] or result["rows"]["only_in_target"]
        for p in pairs:
            col_results[p.i] = {"multiset": _column_multiset(side, p) if mismatch
                                else {"only_in_source": 0, "only_in_target": 0, "examples": {}}}

    columns, findings = [], []
    for p in pairs:
        sn = snulls.get(p.source["name"].lower(), {"nulls": 0, "blanks": 0})
        tn = tnulls.get(p.target["name"].lower(), {"nulls": 0, "blanks": 0})
        added_nulls = max(0, tn["nulls"] - sn["nulls"] - sn["blanks"]) if source_rows == target_rows else 0
        c = {"label": _column_label(p), "source": p.source["name"], "target": p.target["name"],
             "stype": p.stype, "ttype": p.ttype, "match": p.match, "mode": p.mode(), "lookup": p.lookup,
             "is_key": key is not None and p.i == key.i,
             "nulls": {"source": sn["nulls"], "source_blanks": sn["blanks"], "target": tn["nulls"],
                       "target_blanks": tn["blanks"], "added": added_nulls},
             "cannot_convert": failures.get(p.i, 0), "rows_equal": source_rows == target_rows,
             **col_results.get(p.i, {})}
        if p.i in lookup_hints:
            c["lookup_hint"] = lookup_hints[p.i]
        c["verdict"], fs = _column_verdict(c)
        findings += [{"severity": s, "text": t, "column": c["label"]} for s, t in fs]
        columns.append(c)
    result["columns"] = columns

    # Row-level findings, most important first.
    r = result["rows"]
    top = []
    if result["method"] == "key":
        if r["missing_in_target"]:
            per = ", ".join(f"{x['table']} {_fmt(x['rows'])}" for x in result["missing_by_source"]) if len(srcs) > 1 else ""
            ex = ", ".join(x["key"] for x in result["missing_examples"][:6])
            top.append(("error", f"{_fmt(r['missing_in_target'])} source rows are missing in the target"
                                 f"{f' ({per})' if per else ''}: {key.target['name']} {ex}"
                                 f"{' …' if r['missing_in_target'] > 6 else ''}."))
        extra = r["extra_in_target"] - r["target_null_keys"]
        if r["target_null_keys"]:
            top.append(("error", f"{_fmt(r['target_null_keys'])} target rows have {key.target['name']} = NULL: "
                                 "rows with no source row."))
        if extra > 0:
            ex = ", ".join(result["extra_examples"][:6])
            top.append(("error", f"{_fmt(extra)} target rows have no source row: {key.target['name']} {ex}"
                                 f"{' …' if extra > 6 else ''}."))
    else:
        if r["only_in_source"] or r["only_in_target"]:
            # Columns whose difference is for review (recoded, case only, filled on load) are
            # left out and the rows compared again, so the row verdict rests on the rest.
            soft = [p for p, c in zip(pairs, columns) if c.get("soft")]
            rest = [p for p in pairs if p not in soft]
            names = ", ".join(_column_label(p) for p in soft)
            plural = "s" if len(soft) != 1 else ""
            if soft and rest:
                r2 = _fingerprint(side, rest)
                r["identical_rows_ignoring_recoded"] = r2["identical_rows"]
                if not r2["only_in_source"] and not r2["only_in_target"]:
                    top.append(("review", f"All {_fmt(r2['identical_rows'])} rows are identical apart from the "
                                          f"column{plural} to review: {names}."))
                else:
                    top.append(("error", f"{_fmt(r2['only_in_source'])} source rows have no identical row in the "
                                         f"target, even leaving out the column{plural} to review ({names})."))
            elif soft:
                top.append(("review", f"Rows differ only in the column{plural} to review: {names}."))
            else:
                top.append(("error", f"{_fmt(r['only_in_source'])} source rows have no identical row in the target, and "
                                     f"{_fmt(r['only_in_target'])} target rows have none in the source."))
    for note in notes:
        if "renumbered" in note:
            top.append(("review", note))
    unpaired = [x["source"]["name"] for x in rows if x["source"] and not x["target"]]
    if unpaired:
        top.append(("info", f"{len(unpaired)} source column{'s' if len(unpaired) != 1 else ''} with no paired target "
                            f"column, so not compared: {', '.join(unpaired[:8])}{' …' if len(unpaired) > 8 else ''}. "
                            "Dropped, or renamed beyond the naming rules (declare those under columns: in the "
                            "mapping file)."))
    if not_compared:
        top.append(("info", f"Not compared (type cannot be compared): {', '.join(not_compared)}."))
    heavy = [c for c in tcols if target_rows >= 100 and tnulls[c["name"].lower()]["nulls"] >= 0.9 * target_rows]
    if heavy:
        top.append(("info", f"{len(heavy)} of {len(tcols)} target columns are at least 90% NULL: "
                            f"{', '.join(c['name'] for c in heavy[:8])}{' …' if len(heavy) > 8 else ''}."))
    result["findings"] = [{"severity": s, "text": t} for s, t in top] + findings
    result["profile"] = {
        "source": [{"column": c["name"], "type": c["type"], **snulls[c["name"].lower()]} for c in source_cols[0]],
        "target": [{"column": c["name"], "type": c["type"], **tnulls[c["name"].lower()]} for c in tcols],
        "source_rows": source_rows, "target_rows": target_rows,
    }

    errors = sum(1 for f in result["findings"] if f["severity"] == "error")
    reviews = sum(1 for f in result["findings"] if f["severity"] == "review")
    compared = len(pairs)
    # The headline is the most important finding itself, so a list of mappings reads as a
    # list of what is wrong; the counts are kept alongside.
    first = next((f["text"] for f in result["findings"] if f["severity"] == "error"), None)
    if result["method"] == "fingerprint":
        # Without a key the row summary only says "N rows differ"; the column says why.
        first = next((f["text"] for f in result["findings"] if f["severity"] == "error" and f.get("column")), first)
    if errors:
        result["status"] = "problems"
        result["headline"] = first
    elif reviews:
        result["status"] = "review"
        result["headline"] = next(f["text"] for f in result["findings"] if f["severity"] == "review")
    else:
        result["status"] = "identical"
        result["headline"] = f"All {_fmt(r.get('identical_rows', 0))} rows identical across {compared} compared columns."
    return result


# ---- results store and background run ---------------------------------------------------------

class Store:
    """Last result per mapping, kept in memory and in a JSON file so it survives a restart."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.results = {}
        self.job = {"running": False, "done": 0, "total": 0, "current": None, "started_at": None, "finished_at": None}
        try:
            self.results = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.results = {}

    def put(self, result):
        with self.lock:
            self.results[result["mapping"]] = result
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.results, default=str), encoding="utf-8")
            except OSError:
                pass

    def get(self, mapping_id):
        return self.results.get(mapping_id)

    def summaries(self):
        keep = ("mapping", "status", "headline", "checked_at", "seconds", "method")
        out = {}
        for mid, r in self.results.items():
            s = {k: r.get(k) for k in keep}
            s["errors"] = sum(1 for f in r.get("findings", []) if f["severity"] == "error")
            s["reviews"] = sum(1 for f in r.get("findings", []) if f["severity"] == "review")
            out[mid] = s
        return out

    def run_all(self, mappings, run_one):
        with self.lock:
            if self.job["running"]:
                return False
            self.job = {"running": True, "done": 0, "total": len(mappings), "current": None,
                        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "finished_at": None}

        def work():
            try:
                for m in mappings:
                    self.job["current"] = m.id
                    try:
                        self.put(run_one(m))
                    except Exception as exc:  # one failed mapping must not stop the run
                        self.put({"mapping": m.id, "type": m.type, "status": "error",
                                  "headline": f"Check failed: {exc}",
                                  "findings": [{"severity": "error", "text": f"Check failed: {exc}"}],
                                  "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    self.job["done"] += 1
            finally:
                self.job.update(running=False, current=None,
                                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

        threading.Thread(target=work, daemon=True, name="data-check").start()
        return True
