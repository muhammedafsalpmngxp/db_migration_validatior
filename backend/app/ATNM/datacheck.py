"""Data check of one table: are the rows on RDS the same as on ATNM?

The two databases live on different servers, so nothing can be joined across them. Each
server instead reduces its own table to a few numbers, and only those numbers travel:

1. Fingerprint. Every value is turned into text the same way on both servers (dates as
   ISO 8601, floats with every digit, binary as hex, NULL as a marker) and each row is
   hashed with SHA-256 - in pieces of at most 4,000 characters, cut identically on both
   servers, so no row is ever handled as a large object. The table's fingerprint is its
   row count plus two sums of the row hashes - the same on both sides exactly when both
   hold the same rows, in any order and with the same duplicates. Equal fingerprints: the
   table is identical.
2. Which columns differ: the same fingerprint per column, plus how many values are filled.
3. Which rows differ: rows are split into 1,024 groups (by their key when the table has a
   primary key or a unique index, else by their own hash) and each group is fingerprinted.
   Only the groups that differ are read row by row - key and hash, never the values - up
   to ATNM_DIFF_ROWS_MAX rows per side, and compared here: rows missing on RDS, extra on
   RDS, and (with a key) rows whose values changed.
4. A few examples of each, with the values of the columns that differ.

Everything is read-only and READ UNCOMMITTED, like the rest of the app. timestamp /
rowversion columns are left out: the server generates them, so they always differ.
"""
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
from datetime import datetime, timezone

import pyodbc

from .. import identity
from ..values import HIDDEN, is_sensitive
from . import conn, settings, structure

SKIP_TYPES = {"timestamp", "rowversion"}
TEXT = {"char", "varchar", "text", "nchar", "nvarchar", "ntext", "sysname", "xml"}
DATES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
BINARY = {"binary", "varbinary", "image", "geography", "geometry", "hierarchyid"}
FLOATS = {"float", "real"}
MONEY = {"money", "smallmoney"}
GROUPS = 1024
EXAMPLES = 5
VALUE_CHARS = 200
NULL = "NCHAR(9216)"        # stands for NULL in the row text
SEP = "NCHAR(31)"           # between two values of a row
SEP_CHAR = "\x1f"
NULL_CHAR = "␀"


class Stopped(Exception):
    """The run was cancelled."""


class SideError(Exception):
    """A connection or query failed on one of the two servers."""

    def __init__(self, side, error):
        super().__init__(str(error))
        self.side, self.error = side, error


class Skipped(Exception):
    """The table cannot be checked; the message says why."""


def _q(name):
    return "[" + name.replace("]", "]]") + "]"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt(n):
    return f"{n:,}"


# ---- how a value becomes text -------------------------------------------------------------

LOB = {"text", "ntext", "xml", "image", "geography", "geometry", "hierarchyid", "sql_variant"}
CHUNK_CHARS = 4000      # most characters in one piece of a row: never a large object, so hashing stays fast
CHUNK_COLUMNS = 100     # CONCAT takes at most 254 arguments (values and separators)
BATCH_COLUMNS = 40      # columns per query when fingerprinting columns one by one


def _width(col):
    """Most characters the column's value can take as text, or None for a large object."""
    b, n = col["base"], col.get("max_length")
    if n == -1 or b in LOB:
        return None
    if b in ("nchar", "nvarchar", "sysname"):
        w = n // 2
    elif b in ("char", "varchar"):
        w = n
    elif b in ("binary", "varbinary"):
        w = 2 + 2 * n
    elif b in ("decimal", "numeric"):
        w = 42
    elif b == "uniqueidentifier":
        w = 36
    else:   # dates, times, floats, money, whole numbers, bit
        w = 40
    return max(1, w) if w <= CHUNK_CHARS else None


class Column:
    """One compared column: its definition on each side, and how wide its text can be. The
    width is the same on both sides, so both servers cut a row into the same pieces."""

    def __init__(self, row):
        self.name = row["name"]
        self.source, self.target = row["source"], row["target"]
        ws, wt = _width(self.source), _width(self.target)
        self.lob = ws is None or wt is None
        self.width = None if self.lob else max(ws, wt)


class _OneColumn:
    """A column only one of the two tables has (a rename candidate): its own text width."""

    def __init__(self, col):
        self.name = col["name"]
        w = _width(col)
        self.lob = w is None
        self.width = w


BLANKABLE = {"char", "varchar", "nchar", "nvarchar"}


def _chunks(columns, idxs=None):
    """Column indexes grouped into pieces of at most CHUNK_CHARS characters; a large object
    is a piece of its own."""
    out, cur, size = [], [], 0
    for i in (range(len(columns)) if idxs is None else idxs):
        c = columns[i]
        if c.lob:
            out.append([i])
            continue
        if cur and (size + c.width + 1 > CHUNK_CHARS or len(cur) >= CHUNK_COLUMNS):
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += c.width + 1
    if cur:
        out.append(cur)
    return out


class Dialect:
    """The SQL both servers can run. SQL Server 2016+ hashes large values in full and keeps
    every digit of a float; older servers hash a large value's first 4,000 characters."""

    def __init__(self, major_source, major_target):
        self.modern = min(major_source or 0, major_target or 0) >= 13
        self.float_style = 3 if self.modern else 2

    def value(self, col, column, alias="x"):
        """The value as text: `col` is this side's definition, `column` the shared one."""
        c = f"{alias}.{_q(col['name'])}"
        b = col["base"]
        n = "max" if column.lob else str(column.width)
        if b in DATES:
            e = f"CONVERT(nvarchar(40), {c}, 126)"
        elif b in FLOATS:
            e = f"CONVERT(nvarchar(40), {c}, {self.float_style})"
        elif b in MONEY:
            e = f"CONVERT(nvarchar(40), {c}, 2)"
        elif b in BINARY:
            e = (f"CONVERT(nvarchar(max), CAST({c} AS varbinary(max)), 1)" if column.lob
                 else f"CONVERT(nvarchar({n}), {c}, 1)")
        elif b == "sql_variant":
            e = f"CONVERT(nvarchar(4000), {c})"
        else:   # text, whole and decimal numbers, bit, uniqueidentifier, time
            e = f"CONVERT(nvarchar({n}), {c})"
        return f"ISNULL({e}, {NULL})"

    def hash(self, text_expr, lob=False):
        if self.modern or not lob:
            return f"HASHBYTES('SHA2_256', {text_expr})"
        return f"HASHBYTES('SHA2_256', CAST(SUBSTRING({text_expr}, 1, 4000) AS nvarchar(4000)))"

    def text(self, cols, columns, idxs, alias="x"):
        """The values of `idxs` as one text, separated (CONCAT keeps the expression flat)."""
        parts = [self.value(cols[i], columns[i], alias) for i in idxs]
        return parts[0] if len(parts) == 1 else "CONCAT(" + f", {SEP}, ".join(parts) + ")"

    def row_hash(self, cols, columns, chunks, alias="x"):
        hs = [self.hash(self.text(cols, columns, ch, alias), any(columns[i].lob for i in ch)) for ch in chunks]
        if len(hs) == 1:
            return hs[0]
        if len(hs) > 250:
            raise Skipped("The rows are too wide to hash in one piece (over 250 large or long columns).")
        return "HASHBYTES('SHA2_256', " + " + ".join(f"CAST({h} AS varbinary(32))" for h in hs) + ")"


def _sum(slice_start, expr="h"):
    return f"SUM(CAST(CAST(SUBSTRING({expr}, {slice_start}, 7) AS bigint) AS decimal(38, 0)))"


def _group(expr):
    return f"CAST(CAST(SUBSTRING({expr}, 1, 4) AS bigint) % {GROUPS} AS int)"


# ---- one side of the comparison -----------------------------------------------------------

class Side:
    """One table on one server, with the SQL that reduces it."""

    def __init__(self, server, database, entry, cols, columns, key_idx, dialect, cutoff=None):
        self.server, self.database, self.entry = server, database, entry
        self.cols = cols            # this side's definition of each compared column, in the shared order
        self.columns = columns      # the shared Column of each
        self.key_idx = key_idx      # indexes of the row key columns, or []
        self.d = dialect
        self.chunks = _chunks(columns)
        self.table = f"{_q(entry['schema'])}.{_q(entry['table'])}"
        if cutoff:
            # Only the rows up to the cutoff, the same on both servers (a live table keeps growing).
            self.table = (f"(SELECT * FROM {self.table} WHERE {_q(cutoff['column'])} <= "
                          f"CONVERT(datetime2, '{cutoff['value']}', 126))")

    @property
    def h(self):
        return self.d.row_hash(self.cols, self.columns, self.chunks)

    @property
    def k(self):
        return self.d.text(self.cols, self.columns, self.key_idx) if self.key_idx else None

    def v(self, i):
        return self.d.value(self.cols[i], self.columns[i])

    def base(self):
        """h (row hash), k (key text) and g (group) of every row."""
        if self.key_idx:
            inner = f"SELECT {self.h} AS h, {self.k} AS k FROM {self.table} x"
            return f"(SELECT h, k, {_group(self.d.hash('k'))} AS g FROM ({inner}) y)"
        inner = f"SELECT {self.h} AS h FROM {self.table} x"
        return f"(SELECT h, {_group('h')} AS g FROM ({inner}) y)"

    def fingerprint_sql(self):
        return (f"SELECT COUNT_BIG(*) AS n, {_sum(1)} AS s1, {_sum(8)} AS s2 "
                f"FROM (SELECT {self.h} AS h FROM {self.table} x) q")

    def columns_sql(self, idxs):
        parts = []
        for i in idxs:
            parts.append(f"{_sum(1, self.d.hash(self.v(i), self.columns[i].lob))} AS f{i}")
            parts.append(f"SUM(CASE WHEN x.{_q(self.cols[i]['name'])} IS NULL THEN 0 ELSE 1 END) AS n{i}")
        return f"SELECT {', '.join(parts)} FROM {self.table} x"

    def groups_sql(self):
        return f"SELECT g, COUNT_BIG(*) AS n, {_sum(1)} AS s FROM {self.base()} q GROUP BY g"

    def rows_sql(self, groups):
        # A semi-join, not IN (1, 2, ...): a long IN list repeats the hash expression once per
        # value and overflows the query compiler (error 8632) on wide tables.
        where = f"g IN (SELECT v FROM (VALUES {', '.join(f'({int(g)})' for g in groups)}) t(v))"
        if self.key_idx:
            return f"SELECT k, h FROM {self.base()} q WHERE {where}"
        return f"SELECT h, COUNT_BIG(*) AS n FROM {self.base()} q WHERE {where} GROUP BY h"

    def values_by_key_sql(self, n):
        parts = [f"{self.k} AS k"]
        for i in range(len(self.cols)):
            parts.append(f"LEFT({self.v(i)}, {VALUE_CHARS}) AS v{i}")
            parts.append(f"{self.d.hash(self.v(i), self.columns[i].lob)} AS x{i}")
        marks = ", ".join("?" for _ in range(n))
        return f"SELECT {', '.join(parts)} FROM {self.table} x WHERE {self.k} IN ({marks})"

    def profile_sql(self):
        """Rows, and NULL (and, for text, blank) values of every column of this side's table."""
        parts = ["COUNT_BIG(*) AS __rows"]
        for i, c in enumerate(self.entry["columns"]):
            col = f"x.{_q(c['name'])}"
            parts.append(f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS n{i}")
            if c["base"] in BLANKABLE:
                parts.append(f"SUM(CASE WHEN {col} IS NOT NULL AND LTRIM(RTRIM({col})) = N'' THEN 1 ELSE 0 END) AS b{i}")
        return f"SELECT {', '.join(parts)} FROM {self.table} x"

    def pairing_sql(self, only):
        """For every row: what pairs it with its copy (the key, or else the shared columns),
        and a hash and a filled flag of each column in `only` (columns this side alone has)."""
        p = self.d.hash(self.k) if self.key_idx else self.h
        parts = [f"{p} AS p"]
        for i, c in enumerate(only):
            one = _OneColumn(c)
            parts.append(f"CAST(SUBSTRING({self.d.hash(self.d.value(c, one), one.lob)}, 1, 8) AS bigint) AS v{i}")
            parts.append(f"CASE WHEN x.{_q(c['name'])} IS NULL THEN 0 ELSE 1 END AS f{i}")
        return f"SELECT {', '.join(parts)} FROM {self.table} x"

    def values_by_hash_sql(self, n):
        parts = [f"LEFT({self.v(i)}, {VALUE_CHARS}) AS v{i}" for i in range(len(self.cols))]
        marks = ", ".join("?" for _ in range(n))
        return (f"SELECT TOP ({EXAMPLES}) {', '.join(parts)} FROM {self.table} x "
                f"CROSS APPLY (SELECT {self.h} AS h) a WHERE a.h IN ({marks})")


class Context:
    """A run in progress: lets a cancel stop the queries that are running, and tells
    `on_note` (when set) what the check is doing."""

    def __init__(self, on_note=None, on_pass=None):
        self.cancelled = False
        self.lock = threading.Lock()
        self.cursors = set()
        self.on_note = on_note
        self.on_pass = on_pass      # on_pass(event, **facts): a full read of a table starts / ends
        self.stopped = threading.Event()

    def note(self, text, level="info", step=False):
        """What the check is doing now (`step=True`) or has just done."""
        if self.on_note:
            self.on_note(text, level, step)

    def pass_start(self, n, of, label, rows):
        """Full read `n` of about `of` over a table of `rows` rows begins."""
        if self.on_pass:
            self.on_pass("start", n=n, of=of, label=label, rows=rows)

    def pass_end(self, rows, seconds):
        """That read ended after `seconds` (the slower of the two servers)."""
        if self.on_pass:
            self.on_pass("end", rows=rows, seconds=seconds)

    def wait(self, seconds):
        """Sleep, but wake up at once on a cancel. True when cancelled."""
        return self.stopped.wait(seconds) or self.cancelled

    def cancel(self):
        self.stopped.set()
        with self.lock:
            self.cancelled = True
            for cur in list(self.cursors):
                try:
                    cur.cancel()
                except pyodbc.Error:
                    pass

    def check(self):
        if self.cancelled:
            raise Stopped("Cancelled.")


class _Runner:
    """Both sides of one table, each on its own connection, queried side by side."""

    def __init__(self, src, tgt, ctx):
        self.src, self.tgt, self.ctx = src, tgt, ctx
        self.stack = ExitStack()
        self.cons = {}
        self.passes = {"n": 1, "of": 1, "rows": 0}     # full reads of the table: done so far / expected

    def timed_pass(self, label, fn):
        """One more full read of the table: reported before and after, for progress and ETA."""
        p = self.passes
        p["n"] = min(p["n"] + 1, p["of"])
        self.ctx.pass_start(p["n"], p["of"], label, p["rows"])
        started = time.time()
        out = fn()
        self.ctx.pass_end(p["rows"], time.time() - started)
        return out

    def __enter__(self):
        for side in (self.src, self.tgt):
            try:
                con = conn.connect(side.server, side.database)
            except (pyodbc.Error, conn.NotConfigured) as exc:
                self.stack.close()
                raise SideError(side, exc) from exc
            self.cons[id(side)] = self.stack.enter_context(closing(con))
        return self

    def __exit__(self, *exc):
        self.stack.close()

    def one(self, side, sql, params=()):
        self.ctx.check()
        cur = self.cons[id(side)].cursor()
        with self.ctx.lock:
            self.ctx.cursors.add(cur)
        try:
            return conn.fetch(cur, sql, params)
        except pyodbc.Error as exc:
            self.ctx.check()        # a cancelled query surfaces as a driver error
            raise SideError(side, exc) from exc
        finally:
            with self.ctx.lock:
                self.ctx.cursors.discard(cur)

    def both(self, make_sql, params=None, what=None):
        """make_sql(side) run on the ATNM side and the RDS side at the same time. With `what`,
        each server's finish is noted with how long it took."""
        def timed(side, sql, args):
            started = time.time()
            rows = self.one(side, sql, args)
            if what:
                self.ctx.note(f"{side.server.label} ({side.database}) finished {what} in {_took(time.time() - started)}.")
            return rows

        with ThreadPoolExecutor(max_workers=2) as pool:
            fs = pool.submit(timed, self.src, make_sql(self.src), (params or {}).get("src", ()))
            ft = pool.submit(timed, self.tgt, make_sql(self.tgt), (params or {}).get("tgt", ()))
            return fs.result(), ft.result()


def _took(seconds):
    """3.2 s, 4 min 05 s, 1 h 02 min"""
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


# ---- the check ----------------------------------------------------------------------------

def _shared_columns(rows):
    """Columns both tables have, in name order, minus the ones generated by the server."""
    shared, skipped = [], []
    for r in sorted((r for r in rows if r["source"] and r["target"]), key=lambda r: r["name"].lower()):
        if r["source"]["base"] in SKIP_TYPES or r["target"]["base"] in SKIP_TYPES:
            skipped.append(f"{r['name']} ({r['source']['type']}: generated by the server, always different)")
            continue
        shared.append(r)
    return shared, skipped


def _key_text(k):
    return None if k is None else k.replace(SEP_CHAR, " · ")


def _value(v, hide):
    if v is None or v == NULL_CHAR:
        return None
    return HIDDEN if hide else v


# The status of a check that could not finish, by the kind of failure (app.ATNM.conn.kind).
FAILED_STATUS = {"timeout": "timeout", "changed": "changed", "locked": "locked"}


def check_table(pair, src_entry, tgt_entry, src_server_info, tgt_server_info, ctx=None, cutoff=None):
    """Data check of one table of `pair`. Entries come from app.ATNM.catalog; `cutoff`
    ({column, value}) limits both sides to the rows up to that moment.

    A check that cannot finish carries `error_kind` (connection, timeout, changed, login,
    config, other) so the run can decide what to do: wait and retry, or report and go on."""
    ctx = ctx or Context()
    started = time.time()
    rows = structure.compare_columns(src_entry["columns"], tgt_entry["columns"])
    result = {"pair": pair.id, "key": src_entry["key"], "schema": src_entry["schema"], "table": src_entry["table"],
              "checked_at": _now(), "signature": structure.signature(rows) + (f"|cutoff:{cutoff}" if cutoff else ""),
              "cutoff": cutoff}
    try:
        result.update(_check(pair, src_entry, tgt_entry, rows, src_server_info, tgt_server_info, ctx, cutoff))
    except Skipped as exc:
        result.update(status="skipped", headline=str(exc), findings=[{"severity": "info", "text": str(exc)}])
    except conn.Locked as exc:
        result.update(status="locked", error_kind="locked", headline=str(exc),
                      findings=[{"severity": "info", "text": str(exc)}])
    except Stopped:
        raise
    except SideError as exc:
        what = conn.kind(exc.error)
        message, hint = conn.explain(exc.error, exc.side.server, exc.side.database)
        text = f"{exc.side.server.label}: {message}" + (f" {hint}" if hint else "")
        status = FAILED_STATUS.get(what, "error")
        result.update(status=status, error_kind=what, error_server=exc.side.server.key,
                      headline=text if status != "error" else f"Check failed: {text}",
                      findings=[{"severity": "error", "text": text}])
    result["seconds"] = round(time.time() - started, 1)
    return result


def _check(pair, src_entry, tgt_entry, rows, src_info, tgt_info, ctx, cutoff=None):
    n_meta = max(src_entry["rows"], tgt_entry["rows"])
    if settings.DATA_CHECK_MAX_ROWS and n_meta > settings.DATA_CHECK_MAX_ROWS:
        raise Skipped(f"{_fmt(n_meta)} rows, over the {_fmt(settings.DATA_CHECK_MAX_ROWS)} row limit "
                      "(ATNM_DATA_CHECK_MAX_ROWS).")
    shared, not_compared = _shared_columns(rows)
    if not shared:
        raise Skipped("The two tables have no column in common that can be compared.")

    d = Dialect(src_info.get("major"), tgt_info.get("major"))
    columns = [Column(r) for r in shared]
    # The ATNM table's primary key (or unique index) identifies a row, when both tables have
    # its columns and its text is short enough to compare in one piece.
    src_key = (src_entry.get("row_key") or {}).get("columns") or []
    index = {c.name.lower(): i for i, c in enumerate(columns)}
    key_idx = [index.get(c.lower()) for c in src_key]
    if (not key_idx or None in key_idx or any(columns[i].lob for i in key_idx)
            or sum(columns[i].width + 1 for i in key_idx) > CHUNK_CHARS):
        key_idx = []
    key_rows = [shared[i] for i in key_idx]
    if cutoff and not all(any(c["name"].lower() == cutoff["column"].lower() for c in e["columns"])
                          for e in (src_entry, tgt_entry)):
        raise Skipped(f"The cutoff column {cutoff['column']} is not in both tables.")
    src = Side(settings.SOURCE, pair.source_db, src_entry, [r["source"] for r in shared], columns, key_idx, d, cutoff)
    tgt = Side(settings.TARGET, pair.target_db, tgt_entry, [r["target"] for r in shared], columns, key_idx, d, cutoff)
    n_rows = max(src_entry["rows"], tgt_entry["rows"])

    out = {"method": {"modern": d.modern, "key": [r["name"] for r in key_rows] or None,
                      "key_kind": (src_entry.get("row_key") or {}).get("kind") if key_rows else None},
           "compared_columns": len(shared), "not_compared": not_compared,
           "columns_only_in_source": [r["name"] for r in rows if r["source"] and not r["target"]],
           "columns_only_in_target": [r["name"] for r in rows if r["target"] and not r["source"]]}

    # Columns only one table has: maybe the same column under a new name (decided by the data).
    s_only = [r["source"] for r in rows if r["source"] and not r["target"] and r["source"]["base"] not in SKIP_TYPES]
    t_only = [r["target"] for r in rows if r["target"] and not r["source"] and r["target"]["base"] not in SKIP_TYPES]

    ctx.note(f"Connecting to {settings.SOURCE.label} ({pair.source_db}) and {settings.TARGET.label} ({pair.target_db}).",
             step=True)
    with _Runner(src, tgt, ctx) as run:
        if s_only and t_only:
            out["renames"], out["renames_note"] = _find_renames(run, src, tgt, s_only, t_only)
            verified = [r for r in out["renames"] if r["verdict"] == "verified"]
            if verified:
                # A verified rename is compared like any shared column from here on.
                by_s = {c["name"]: c for c in s_only}
                by_t = {c["name"]: c for c in t_only}
                for r in verified:
                    row = {"name": f"{r['source']} → {r['target']}", "source": by_s[r["source"]], "target": by_t[r["target"]]}
                    shared.append(row)
                    columns.append(Column(row))
                    src.cols.append(row["source"])
                    tgt.cols.append(row["target"])
                src.chunks = _chunks(columns)
                tgt.chunks = _chunks(columns)
                renamed_s = {r["source"] for r in verified}
                renamed_t = {r["target"] for r in verified}
                out["columns_only_in_source"] = [n for n in out["columns_only_in_source"] if n not in renamed_s]
                out["columns_only_in_target"] = [n for n in out["columns_only_in_target"] if n not in renamed_t]
                out["compared_columns"] = len(shared)
        key_text =f" Rows are identified by {', '.join(r['name'] for r in key_rows)}." if key_rows else ""
        ctx.note(f"Step 1 of 3 · Fingerprinting every row on both servers: about {_fmt(src_entry['rows'])} rows in "
                 f"{settings.SOURCE.label} and {_fmt(tgt_entry['rows'])} in {settings.TARGET.label}, "
                 f"{len(shared)} columns each.{key_text}", step=True)
        ctx.pass_start(1, 1, "Fingerprinting every row", n_rows)
        t0 = time.time()
        fs, ft = run.both(lambda s: s.fingerprint_sql(), what="the fingerprint")
        ctx.pass_end(n_rows, time.time() - t0)
        fs, ft = fs[0], ft[0]
        n_src, n_tgt = int(fs["n"] or 0), int(ft["n"] or 0)
        out["rows"] = {"source": n_src, "target": n_tgt}
        same = (n_src == n_tgt and (fs["s1"] or 0) == (ft["s1"] or 0) and (fs["s2"] or 0) == (ft["s2"] or 0))
        ctx.note(f"Exact row counts: {_fmt(n_src)} in {settings.SOURCE.label}, {_fmt(n_tgt)} in {settings.TARGET.label}.")
        if cutoff:
            ctx.note(f"Only rows with {cutoff['column']} up to {cutoff['value']} were compared (cutoff).")
        if same:
            ctx.note("The fingerprints are equal: every value of every row is identical.", "ok")
        else:
            ctx.note("The fingerprints differ: the tables are not identical. Looking for what differs.", "warn")
        out["profile"] = _profile(run, src, tgt)
        out["identity"] = _identity(run, tgt)
        findings = []
        for r in out.get("renames") or []:
            if r["verdict"] == "verified":
                findings.append({"severity": "info", "text": f"{r['source']} was renamed to {r['target']}: identical on all "
                                                             f"{_fmt(r['paired'])} paired rows, so it is compared as one column."})
            else:
                findings.append({"severity": "review", "text": f"Possible rename {r['source']} → {r['target']}, not "
                                                               f"confirmed: {r['reason']}."})
        if out.get("renames_note"):
            findings.append({"severity": "info", "text": out["renames_note"]})
        for s in out["identity"]:
            if s["behind"]:
                findings.append({"severity": "problem", "text": identity.text(f"{settings.TARGET.label} "
                                                                              f"{tgt.entry['schema']}.{tgt.entry['table']}", s)})
        empty = _mostly_empty(out["profile"])
        if empty:
            findings.append({"severity": "info", "text": empty})
        if out["columns_only_in_source"] or out["columns_only_in_target"]:
            findings.append({"severity": "info", "text": f"Compared on the {len(shared)} columns both tables have; "
                                                         "the others are listed under Columns."})
        if not_compared:
            findings.append({"severity": "info", "text": "Not compared: " + "; ".join(not_compared) + "."})
        if same:
            out["status"] = "identical"
            out["headline"] = f"All {_fmt(n_src)} rows identical across {len(shared)} columns."
            out["findings"] = [{"severity": "ok", "text": out["headline"]}] + findings
            return out

        out["status"] = "different"
        batches = -(-len(shared) // BATCH_COLUMNS)
        # The reads still to come: columns (one per batch), groups, rows, examples.
        run.passes = {"n": 1, "of": 1 + batches + 3, "rows": n_rows}
        out["columns"] = _column_diffs(run, shared)
        out["diff"], out["examples"] = _row_diffs(run, src, tgt, shared, bool(key_rows))

    diff, cols = out["diff"], out["columns"]
    at_least = "at least " if diff["partial"] else ""
    if key_rows:
        parts = []
        if diff["missing"]:
            parts.append(f"{at_least}{_fmt(diff['missing'])} missing in RDS")
        if diff["extra"]:
            parts.append(f"{at_least}{_fmt(diff['extra'])} extra in RDS")
        if diff["changed"]:
            parts.append(f"{at_least}{_fmt(diff['changed'])} with changed values")
        headline = "Rows differ: " + (", ".join(parts) if parts else "the values differ") + "."
    else:
        headline = (f"{at_least}{_fmt(diff['only_in_source'])} ATNM rows have no identical row in RDS, and "
                    f"{at_least}{_fmt(diff['only_in_target'])} RDS rows none in ATNM.")
    if cols:
        headline += " Columns that differ: " + ", ".join(c["name"] for c in cols[:5]) + (" …" if len(cols) > 5 else "") + "."
    out["headline"] = headline
    findings.insert(0, {"severity": "problem", "text": headline})
    if n_src != n_tgt:
        findings.insert(1, {"severity": "problem", "text": f"Exact row counts: {_fmt(n_src)} in ATNM, {_fmt(n_tgt)} in RDS."})
    for c in cols:
        text = f"{c['name']}: values differ"
        if c["filled_source"] != c["filled_target"]:
            text += (f" — filled in {_fmt(c['filled_source'])} rows in ATNM, {_fmt(c['filled_target'])} in RDS")
        findings.append({"severity": "problem", "text": text + "."})
    if diff["partial"]:
        findings.append({"severity": "info", "text": f"Only {diff['groups_examined']} of the {diff['groups_differing']} "
                                                     "groups of rows that differ were read row by row "
                                                     f"(ATNM_DIFF_ROWS_MAX = {_fmt(settings.DIFF_ROWS_MAX)}), so the "
                                                     "row counts above are a minimum."})
    out["findings"] = findings
    return out


def _profile(run, src, tgt):
    """NULL and blank values of every column on both servers (one read of each table)."""
    run.ctx.note("Counting the empty values (NULL and blank) of every column on both servers.", step=True)
    a, b = run.both(lambda s: s.profile_sql(), what="counting empty values")
    a, b = a[0], b[0]

    def per(side, r):
        out = {}
        for i, c in enumerate(side.entry["columns"]):
            out[c["name"].lower()] = (c["name"], {"nulls": int(r[f"n{i}"] or 0),
                                                  "blanks": int(r[f"b{i}"] or 0) if f"b{i}" in r else None})
        return out

    ps, pt = per(src, a), per(tgt, b)
    columns = []
    for k, (name, v) in ps.items():
        columns.append({"name": name, "source": v, "target": pt[k][1] if k in pt else None})
    for k, (name, v) in pt.items():
        if k not in ps:
            columns.append({"name": name, "source": None, "target": v})
    return {"rows": {"source": int(a["__rows"] or 0), "target": int(b["__rows"] or 0)}, "columns": columns}


def _identity(run, side):
    """The identity counter of each identity column of the RDS copy (app/identity.py)."""
    out = []
    for c in side.entry["columns"]:
        if not c.get("identity"):
            continue
        sql, params = identity.query(side.entry["schema"], side.entry["table"], c["name"])
        rows = run.one(side, sql, params)
        if rows:
            out.append(identity.state(c["name"], rows[0]))
    return out


def _mostly_empty(profile, share=0.9, min_rows=100):
    """The RDS columns that are at least 90% empty (the rule the RDS data check uses too)."""
    n = profile["rows"]["target"]
    if n < min_rows:
        return None
    heavy = [c["name"] for c in profile["columns"]
             if c["target"] and c["target"]["nulls"] + (c["target"]["blanks"] or 0) >= share * n]
    if not heavy:
        return None
    total = sum(1 for c in profile["columns"] if c["target"])
    return (f"{len(heavy)} of {total} RDS columns are at least {share:.0%} empty: {', '.join(heavy[:8])}"
            f"{' …' if len(heavy) > 8 else ''}.")


def _find_renames(run, src, tgt, s_only, t_only):
    """Pair columns only ATNM has with columns only RDS has, by their data.

    Rows are paired on the row key (else on the columns both tables share, where that
    combination is unique on both sides); only keys, value hashes and filled flags are
    read. A pair is `verified` under the RDS rename check's rules: identical on every
    paired row, at least RENAME_MIN_VALUES filled values (half the paired rows on a small
    table), more than one value, at least RENAME_MIN_COVERAGE of the smaller table paired,
    and no other column matching as well. Anything less that is identical on at least
    RENAME_POSSIBLE_MIN of the rows is `possible` (or `ambiguous`): a person decides.
    Returns (decisions, note)."""
    n_rows = max(src.entry["rows"], tgt.entry["rows"])
    if n_rows > settings.DIFF_ROWS_MAX:
        note = (f"Columns only one table has were not checked for renames: {_fmt(n_rows)} rows, over "
                f"ATNM_DIFF_ROWS_MAX ({_fmt(settings.DIFF_ROWS_MAX)}).")
        run.ctx.note(note, "warn")
        return [], note
    how = "the row key" if src.key_idx else "the columns both tables share"
    run.ctx.note(f"Looking for renamed columns: {len(s_only)} column{'s' if len(s_only) != 1 else ''} only in "
                 f"{settings.SOURCE.label}, {len(t_only)} only in {settings.TARGET.label}; rows paired on {how}.", step=True)
    rs, rt = run.both(lambda s: s.pairing_sql(s_only if s is src else t_only), what="reading the rename candidates")

    def index(rows):
        out, dup = {}, set()
        for r in rows:
            if r["p"] in out:
                dup.add(r["p"])
            else:
                out[r["p"]] = r
        for p in dup:
            out.pop(p, None)
        return out

    S, T = index(rs), index(rt)
    paired = [p for p in S if p in T]
    n = len(paired)
    if not n:
        note = f"Columns only one table has could not be checked for renames: no row pairs up on {how}."
        run.ctx.note(note, "warn")
        return [], note
    smaller = min(len(rs), len(rt))
    coverage = n / smaller if smaller else 0.0
    needed = min(settings.RENAME_MIN_VALUES, max(1, n // 2))
    tv = [([T[p][f"v{j}"] for p in paired], [T[p][f"f{j}"] for p in paired]) for j in range(len(t_only))]
    cands = []
    for i, sc in enumerate(s_only):
        sv, sf = [S[p][f"v{i}"] for p in paired], [S[p][f"f{i}"] for p in paired]
        filled = sum(sf)
        if not filled:
            continue                                   # an empty column says nothing
        distinct = len({v for v, f in zip(sv, sf) if f})
        for j, (vals, flags) in enumerate(tv):
            identical = sum(1 for a, b in zip(sv, vals) if a == b)
            both_empty = sum(1 for a, b in zip(sf, flags) if not a and not b)
            nonempty = n - both_empty
            rate = (identical - both_empty) / nonempty if nonempty else 0.0
            cands.append({"i": i, "j": j, "full": identical == n, "rate": rate, "identical": identical,
                          "filled": filled, "distinct": distinct})
    full_s = Counter(c["i"] for c in cands if c["full"])
    full_t = Counter(c["j"] for c in cands if c["full"])
    out, taken_s, taken_t = [], set(), set()
    for c in sorted(cands, key=lambda c: (not c["full"], -c["rate"])):
        if c["i"] in taken_s or c["j"] in taken_t:
            continue
        if not c["full"] and c["rate"] < settings.RENAME_POSSIBLE_MIN:
            continue
        why, ambiguous = [], c["full"] and (full_s[c["i"]] > 1 or full_t[c["j"]] > 1)
        if not c["full"]:
            why.append(f"differs on {_fmt(n - c['identical'])} of {_fmt(n)} paired rows")
        if c["filled"] < needed:
            why.append(f"only {_fmt(c['filled'])} filled values (needs {_fmt(needed)})")
        if c["distinct"] < 2:
            why.append("the same value in every row")
        if coverage < settings.RENAME_MIN_COVERAGE:
            why.append(f"only {coverage:.0%} of the rows pair up (needs {settings.RENAME_MIN_COVERAGE:.0%})")
        if ambiguous:
            why.append("another column matches as well")
        sc, tc = s_only[c["i"]], t_only[c["j"]]
        out.append({"source": sc["name"], "target": tc["name"], "source_type": sc["type"], "target_type": tc["type"],
                    "paired": n, "identical": c["identical"], "differing": n - c["identical"], "filled": c["filled"],
                    "rate": round(c["rate"], 4), "coverage": round(coverage, 4),
                    "verdict": "verified" if not why else ("ambiguous" if ambiguous else "possible"),
                    "reason": "; ".join(why) if why else f"identical on all {_fmt(n)} paired rows"})
        taken_s.add(c["i"])
        taken_t.add(c["j"])
    v = sum(1 for r in out if r["verdict"] == "verified")
    run.ctx.note(f"{v} renamed column{'s' if v != 1 else ''} verified by the data"
                 f"{f', {len(out) - v} to review' if len(out) > v else ''} ({_fmt(n)} rows paired).",
                 "ok" if v else "info")
    return out, None


def _column_diffs(run, shared):
    cs, ct = {}, {}
    idxs = list(range(len(shared)))
    for start in range(0, len(idxs), BATCH_COLUMNS):     # a few columns per query: keeps each one simple
        batch = idxs[start:start + BATCH_COLUMNS]
        span = f"columns {start + 1}–{start + len(batch)} of {len(idxs)}"
        run.ctx.note(f"Step 2 of 3 · Finding which columns differ: fingerprinting {span} on both servers.", step=True)
        a, b = run.timed_pass("Finding which columns differ", lambda: run.both(lambda s: s.columns_sql(batch), what=span))
        cs.update(a[0])
        ct.update(b[0])
    out = []
    for i, r in enumerate(shared):
        if (cs[f"f{i}"] or 0) != (ct[f"f{i}"] or 0) or cs[f"n{i}"] != ct[f"n{i}"]:
            out.append({"name": r["name"], "filled_source": int(cs[f"n{i}"] or 0),
                        "filled_target": int(ct[f"n{i}"] or 0)})
    names = ", ".join(c["name"] for c in out[:8]) + (" …" if len(out) > 8 else "")
    run.ctx.note(f"{len(out)} of {len(shared)} columns differ{': ' + names if out else ''}.", "warn" if out else "info")
    return out


def _row_diffs(run, src, tgt, shared, keyed):
    by = "their key" if keyed else "their own hash (the table has no key)"
    run.ctx.note(f"Step 3 of 3 · Finding which rows differ: splitting the rows into {GROUPS:,} groups by {by} and "
                 "fingerprinting each group on both servers.", step=True)
    gs, gt = run.timed_pass("Fingerprinting groups of rows",
                            lambda: run.both(lambda s: s.groups_sql(), what="the group fingerprints"))
    a = {r["g"]: (int(r["n"]), r["s"]) for r in gs}
    b = {r["g"]: (int(r["n"]), r["s"]) for r in gt}
    differing = sorted(g for g in set(a) | set(b) if a.get(g) != b.get(g))
    chosen, total = [], 0
    for g in differing:
        size = max(a.get(g, (0, 0))[0], b.get(g, (0, 0))[0])
        if total + size > settings.DIFF_ROWS_MAX:
            continue
        chosen.append(g)
        total += size
    diff = {"groups_differing": len(differing), "groups_examined": len(chosen),
            "partial": len(chosen) < len(differing)}
    run.ctx.note(f"{len(differing):,} of {GROUPS:,} groups differ; reading {len(chosen):,} of them row by row"
                 + (f" (up to {total:,} rows per server)" if chosen else "")
                 + (f" - the rest are over ATNM_DIFF_ROWS_MAX ({settings.DIFF_ROWS_MAX:,})" if diff["partial"] else "") + ".")
    examples = {}
    hide = {r["name"] for r in shared if is_sensitive(r["name"])}
    names = [r["name"] for r in shared]
    if not chosen:
        diff.update(missing=0, extra=0, changed=0, only_in_source=0, only_in_target=0)
        return diff, examples

    run.ctx.note(f"Step 3 of 3 · Reading the {len(chosen):,} differing groups row by row (keys and hashes only, no values).",
                 step=True)
    rs, rt = run.timed_pass("Reading the differing rows",
                            lambda: run.both(lambda s: s.rows_sql(chosen), what="reading the differing groups"))
    if keyed:
        # The key is unique in ATNM; RDS may hold it more than once, so keep every hash.
        sk, tk = {}, {}
        for r in rs:
            sk.setdefault(r["k"], []).append(r["h"])
        for r in rt:
            tk.setdefault(r["k"], []).append(r["h"])
        missing = sorted(k for k in sk if k not in tk)
        extra = sorted(k for k in tk if k not in sk)
        changed = sorted(k for k in sk if k in tk and sorted(sk[k]) != sorted(tk[k]))
        diff.update(missing=sum(len(sk[k]) for k in missing), extra=sum(len(tk[k]) for k in extra),
                    changed=len(changed), duplicate_keys=sum(1 for v in tk.values() if len(v) > 1))
        key_hidden = any(names[i] in hide for i in src.key_idx)
        examples["missing_keys"] = [HIDDEN if key_hidden else _key_text(k) for k in missing[:10]]
        examples["extra_keys"] = [HIDDEN if key_hidden else _key_text(k) for k in extra[:10]]
        run.ctx.note(f"Rows found: {diff['missing']:,} missing in {tgt.server.label}, {diff['extra']:,} extra in "
                     f"{tgt.server.label}, {diff['changed']:,} with changed values.", "warn")
        pick = changed[:EXAMPLES]
        if pick:
            run.ctx.note(f"Reading the values of {len(pick)} changed rows, as examples.", step=True)
            vs, vt = run.timed_pass("Reading example values", lambda: run.both(
                lambda s: s.values_by_key_sql(len(pick)), {"src": tuple(pick), "tgt": tuple(pick)}))
            by_s, by_t = {r["k"]: r for r in vs}, {r["k"]: r for r in vt}
            changes = []
            for k in pick:
                s, t = by_s.get(k), by_t.get(k)
                if not s or not t:
                    continue
                cols = [{"name": names[i], "source": _value(s[f"v{i}"], names[i] in hide),
                         "target": _value(t[f"v{i}"], names[i] in hide)}
                        for i in range(len(names)) if s[f"x{i}"] != t[f"x{i}"]]
                changes.append({"key": HIDDEN if key_hidden else _key_text(k), "columns": cols})
            examples["changed"] = changes
    else:
        cs = Counter({r["h"]: int(r["n"]) for r in rs})
        ct = Counter({r["h"]: int(r["n"]) for r in rt})
        only_s, only_t = cs - ct, ct - cs
        diff.update(only_in_source=sum(only_s.values()), only_in_target=sum(only_t.values()))
        run.ctx.note(f"Rows found: {diff['only_in_source']:,} in {src.server.label} with no identical row in "
                     f"{tgt.server.label}, {diff['only_in_target']:,} the other way round.", "warn")
        if only_s or only_t:
            run.ctx.note("Reading a few of those rows, as examples.", step=True)
        for label, counter, side in (("only_in_source_rows", only_s, src), ("only_in_target_rows", only_t, tgt)):
            hashes = [h for h, _ in counter.most_common(EXAMPLES)]
            if not hashes:
                continue
            rows = run.timed_pass("Reading example rows",
                                  lambda: run.one(side, side.values_by_hash_sql(len(hashes)), tuple(hashes)))
            examples[label] = [{names[i]: _value(r[f"v{i}"], names[i] in hide) for i in range(len(names))}
                               for r in rows]
    return diff, examples
