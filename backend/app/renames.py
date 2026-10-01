"""Renamed columns, decided by the data - never by the names alone.

A source column that has no target column of the same name may have been renamed
(qtyactual -> actual_quantity, text -> task_name). Names cannot tell which: start_date,
target_start and actual_start look alike, and only the values do not. So a rename is
confirmed only when the data proves it:

1. Rows are paired: on a key that is unique on both sides and whose values really match
   (the data check's key when it has one, else the source's primary key or unique index,
   proven unique in the target), or - with no key - on the other columns that hold the
   same values on both sides, pairing only rows those values identify uniquely.
2. Every leftover source column is tried against every leftover target column that can
   hold the same kind of value (text, number, date ...). For a big table a fixed sample of
   paired rows goes first: one differing row there already rules a pair out. The sample
   only ever rules out; it never confirms.
3. Every pair still standing is compared on ALL paired rows, with the data check's own
   rule for "identical" (exact text, NULL = NULL, the source converted to the target type).

A pair is **verified** only when 100% of the paired rows are identical, at least
RENAME_MIN_VALUES of them hold a value, the column holds more than one value, at least
RENAME_MIN_COVERAGE of the smaller table's rows could be paired, and no other column
matches it just as well. Anything less is reported, never confirmed: possible (with how
many rows differ and examples), ambiguous (100% with more than one column), or cannot
verify (with the reason). Rows that exist on one side only - different row counts - do
not count against a rename; they are reported beside it, and the data check reports them.

Pairs the names already made (normalized, inferred, declared in the plan) are measured the
same way, so a name guess the data does not support is shown as such.

Read-only, READ UNCOMMITTED, from the target connection (all three databases are on one
server). Results are kept in RENAME_FILE; nothing here changes how any other check pairs
columns.
"""
import difflib
import itertools
import json
import logging
import re
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pyodbc
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import compare, config, db
from . import datacheck as dc

_env = config._env

SAMPLE_ROWS = int(_env("RENAME_SAMPLE_ROWS", "10000"))      # paired rows tried first on a big table
FULL_BELOW = int(_env("RENAME_FULL_BELOW", "200000"))       # tables up to this many rows skip the sample
MIN_VALUES = int(_env("RENAME_MIN_VALUES", "100"))          # filled values a verified pair needs
MIN_COVERAGE = float(_env("RENAME_MIN_COVERAGE", "0.5"))    # share of the smaller table that must pair
POSSIBLE_MIN = float(_env("RENAME_POSSIBLE_MIN", "0.5"))    # below this share a pair is not even shown
BATCH = int(_env("RENAME_BATCH", "25"))                     # pairs measured in one query
MAX_CANDIDATES = int(_env("RENAME_MAX_CANDIDATES", "3000"))
TIMEOUT = int(_env("RENAME_TIMEOUT", "7200"))               # seconds one query may run
RESULT_FILE = Path(_env("RENAME_FILE", config.BACKEND_DIR / ".cache" / "rename_checks.json"))
LOG_FILE = Path(_env("RENAME_LOG_FILE", config.BACKEND_DIR / ".cache" / "renames.log"))
EXAMPLES = 3
CAND_BASE = 10_000      # candidate pair numbers start here, apart from the name pairs (0 ...)

TARGET = config.TARGET_SIDE
NUMBERS = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money", "smallmoney"}


class Skipped(Exception):
    """The mapping cannot be checked; the message says why."""


class Stopped(Exception):
    """The run was cancelled."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt(n):
    return f"{n:,}"


def _took(seconds):
    """3.2 s, 4 min 05 s, 1 h 02 min"""
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


# ---- log: backend console, RENAME_LOG_FILE and the run's activity list ----------------------

logger = logging.getLogger("renames")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _f = logging.Formatter("%(asctime)s  RENAME  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    _h = logging.StreamHandler()
    _h.setFormatter(_f)
    logger.addHandler(_h)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        _fh.setFormatter(_f)
        logger.addHandler(_fh)
    except OSError:
        pass


class Activity:
    def __init__(self, keep=1000):
        self.lock = threading.Lock()
        self.entries = deque(maxlen=keep)
        self.seq = itertools.count(1)
        self.last = 0
        self.step, self.step_started_at = None, None

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.step = self.step_started_at = None

    def add(self, text, level="info", mapping=None, step=False):
        e = {"at": _now(), "level": level, "mapping": mapping, "text": text}
        with self.lock:
            e["seq"] = self.last = next(self.seq)
            self.entries.append(e)
            if step:
                self.step, self.step_started_at = text, e["at"]
        logger.log({"warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO), "%s%s",
                   f"[{mapping}] " if mapping else "", text)

    def since(self, seq):
        with self.lock:
            return [e for e in self.entries if e["seq"] > seq]


log = Activity()


# ---- queries --------------------------------------------------------------------------------

class Context:
    def __init__(self):
        self.cancelled = False
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.cursors = set()

    def cancel(self):
        self.cancelled = True
        self.stopped.set()
        with self.lock:
            for cur in list(self.cursors):
                try:
                    cur.cancel()
                except pyodbc.Error:
                    pass

    def check(self):
        if self.cancelled:
            raise Stopped("Stopped.")


def _run(ctx, sql, params=()):
    """Rows of one read-only query from the target connection, READ UNCOMMITTED, with the
    rename timeout; a lock gives up quickly (dc.Skipped), a cancel stops the query."""
    ctx.check()
    with closing(db.connect(TARGET)) as con:
        con.timeout = TIMEOUT
        cur = con.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        with ctx.lock:
            ctx.cursors.add(cur)
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
        except pyodbc.Error as exc:
            ctx.check()
            if db.is_lock_timeout(exc):
                raise dc.Skipped("A table is locked by another session (a load in progress?).") from exc
            raise
        finally:
            with ctx.lock:
                ctx.cursors.discard(cur)


def _one(ctx, sql, params=()):
    return _run(ctx, sql, params)[0]


# ---- the tables of a mapping ----------------------------------------------------------------

class Prep:
    """The tables of one target of a mapping, their columns and the name pairing."""


def _prepare(m, entry_of, tmember):
    """Like the data check's preparation, for one target, without its row limit (a rename
    is decided on every row, however many)."""
    if m.type == "excluded":
        raise Skipped("Excluded from the migration: nothing to compare.")
    if m.type == "transform":
        raise Skipped("Transform mapping: business logic reshapes the rows, so columns cannot be matched row by row.")
    tentry = entry_of(tmember)
    srcs = m.sources if m.type == "union" else [next((s for s in m.sources if s.role == "driving"), m.sources[0])]
    sentries = [entry_of(s) for s in srcs]
    for mem, e in [(tmember, tentry)] + list(zip(srcs, sentries)):
        if e is None:
            raise Skipped(f"{mem.ref.ref} does not exist.")
        if e["locked"]:
            raise Skipped(f"{mem.ref.ref} is locked by another session (a load in progress?). Try again later.")
    p = Prep()
    p.srcs, p.sentries, p.tmember, p.tentry = srcs, sentries, tmember, tentry
    p.tcols = db.table_columns(TARGET, tentry["object_id"])
    p.source_cols = [db.table_columns(s.ref.side, e["object_id"]) for s, e in zip(srcs, sentries)]
    keys = db.table_keys(TARGET, tentry["object_id"])
    p.outgoing = {f["columns"][0].lower() for f in keys["foreign_keys"]
                  if f["direction"] == "outgoing" and len(f["columns"]) == 1}
    p.pk = next((k for k in keys["keys"] if k["kind"] == "primary"), None)
    p.source_keys = [k["columns"] for s, e in zip(srcs, sentries)
                     for k in db.table_keys(s.ref.side, e["object_id"])["keys"]]
    p.rows, _ = compare.align_columns(p.source_cols[0], p.tcols, target_table=tentry["table"],
                                      declared=m.columns, fk_columns=p.outgoing)
    comparable = [r for r in p.rows if r["source"] and r["target"]
                  and dc._bt(r["source"]["type"]) not in dc.NOCOMPARE and dc._bt(r["target"]["type"]) not in dc.NOCOMPARE]
    p.pairs = [dc.Pair(i, r) for i, r in enumerate(comparable)]
    p.side = dc.Side([(s, e, {c["name"].lower(): c for c in cols}) for s, e, cols in zip(srcs, sentries, p.source_cols)],
                     (tmember, tentry, p.tcols), p.pairs)
    p.source_rows = sum(e["rows"] for e in sentries)
    p.target_rows = tentry["rows"]
    return p


def signature(p):
    """The columns a result was measured on; another signature means the result is out of date."""
    cols = [f"s:{c['name'].lower()}:{c['type'].lower()}" for cols in p.source_cols for c in cols]
    cols += [f"t:{c['name'].lower()}:{c['type'].lower()}" for c in p.tcols]
    return "|".join(sorted(cols))


# ---- which pairs to try -----------------------------------------------------------------------

def _family(col):
    b = dc._bt(col["type"])
    if b in dc.TEXT:
        return "text"
    if b in NUMBERS:
        return "number"
    if b in dc.DATES:
        return "date"
    return b        # bit, uniqueidentifier, binary, time ...


def _compatible(s, t):
    """Can the two columns hold the same values? Text may hold numbers and dates written out."""
    if dc._bt(s["type"]) in dc.NOCOMPARE or dc._bt(t["type"]) in dc.NOCOMPARE:
        return False
    fs, ft = _family(s), _family(t)
    return fs == ft or "text" in (fs, ft)


_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def name_score(a, b):
    """How alike two names are (0..1) - used only to list candidates in a sensible order,
    never to decide."""
    na, nb = compare.normalize(a), compare.normalize(b)
    wa = {w.lower() for w in _WORDS.findall(a.replace("_", " "))}
    wb = {w.lower() for w in _WORDS.findall(b.replace("_", " "))}
    words = len(wa & wb) / max(1, len(wa | wb))
    return round(max(difflib.SequenceMatcher(None, na, nb).ratio(), words), 3)


def _candidates(p):
    """(name pairs to verify, leftover source x leftover target pairs to try)."""
    named = [r for r in p.rows if r["source"] and r["target"] and r["match"] in ("normalized", "inferred", "declared")
             and _compatible(r["source"], r["target"])]
    left_s = [r["source"] for r in p.rows if r["source"] and not r["target"]]
    left_t = [r["target"] for r in p.rows if r["target"] and not r["source"]]
    cands = [{"source": s, "target": t, "match": None} for s in left_s for t in left_t if _compatible(s, t)]
    cands.sort(key=lambda r: -name_score(r["source"]["name"], r["target"]["name"]))
    return named, cands, left_s, left_t


# ---- pairing the rows ---------------------------------------------------------------------------

def _saved_key(m, p):
    """The key of the mapping's last data check, when it had one."""
    try:
        from . import main as app_main
        saved = app_main._checks.get(m.id)
    except Exception:
        saved = None
    k = (saved or {}).get("key")
    if not k or (saved or {}).get("status") in ("skipped", "error"):
        return None
    return next((x for x in p.pairs if x.source["name"] == k["source"] and x.target["name"] == k["target"]), None)


def _pick_key(ctx, m, p):
    """A name-paired column that identifies a row on both sides, proven by the data.
    Returns (pair, how) or (None, reason)."""
    key = _saved_key(m, p)
    if key:
        return key, f"{key.source['name']} → {key.target['name']} (the data check's key)"
    source_unique = {c[0].lower() for c in p.source_keys if len(c) == 1}
    target_pk = [c.lower() for c in p.pk["columns"]] if p.pk else []
    def keyish(x):
        n = x.target["name"].lower()
        return (x.target["pk"] or x.target["identity"] or x.source["name"].lower() in source_unique
                or n in ("id", "row_id", "uid") or n.endswith("_id")) and n not in p.outgoing
    order = [x for x in p.pairs if [x.target["name"].lower()] == target_pk]
    order += [x for x in p.pairs if x not in order and keyish(x)]
    reasons = []
    for x in order[:6]:
        label = f"{x.source['name']} → {x.target['name']}"
        side = dc.Side(p.side.sources, p.side.target, [x])
        src, tgt = side.source_sql(), side.target_sql()
        s_unique = x.source["name"].lower() in source_unique and len(p.srcs) == 1
        t_unique = [x.target["name"].lower()] == target_pk
        parts = []
        if not s_unique:
            parts.append(f"(SELECT COUNT_BIG(c{x.i}) - COUNT_BIG(DISTINCT c{x.i}) FROM {src} s) AS sdup")
        if not t_unique:
            parts.append(f"(SELECT COUNT_BIG(t{x.i}) - COUNT_BIG(DISTINCT t{x.i}) FROM {tgt} t) AS tdup")
        if parts:
            log.add(f"Checking that {label} identifies each row on both sides (every value used once).",
                    mapping=m.id, step=True)
            r = _one(ctx, "SELECT " + ", ".join(parts))
            if int(r.get("sdup") or 0) or int(r.get("tdup") or 0):
                reasons.append(f"{label} is not unique ({_fmt(int(r.get('sdup') or 0))} repeated values in the source, "
                               f"{_fmt(int(r.get('tdup') or 0))} in the target)")
                continue
        # The values must really match up (a renumbered id pairs nothing): try a sample.
        r = _one(ctx, f"""WITH s AS {src}, t AS {tgt},
                          k AS (SELECT TOP ({SAMPLE_ROWS}) c{x.i} AS v FROM s WHERE c{x.i} IS NOT NULL ORDER BY c{x.i})
                          SELECT (SELECT COUNT_BIG(*) FROM k) AS n,
                                 (SELECT COUNT_BIG(*) FROM k JOIN t ON t.t{x.i} = k.v) AS hit""")
        n, hit = int(r["n"] or 0), int(r["hit"] or 0)
        if not n or hit < 0.5 * n:
            reasons.append(f"{label}: only {_fmt(hit)} of {_fmt(n)} sampled values are found in the target "
                           "(renumbered?)")
            continue
        return x, f"{label} (unique on both sides; {_fmt(hit)} of {_fmt(n)} sampled values match)"
    return None, "; ".join(reasons) or "no column that could identify a row"


def _anchors(ctx, p):
    """With no key: the name-paired columns that hold the same values on both sides (the key
    mapping check's method). Rows are paired on them where their values are unique."""
    from . import keymap
    p.pairs_for_anchor = p.pairs
    return keymap._anchors(p, [])


# ---- measuring ----------------------------------------------------------------------------------

def _measure(ctx, p, key, anchors, pairs, sample):
    """For each pair: paired rows, identical rows, filled source values, and whether the
    column holds more than one value - on all paired rows, or on `sample` of them."""
    out = {}
    for start in range(0, len(pairs), BATCH):
        ctx.check()
        batch = pairs[start:start + BATCH]
        side = dc.Side(p.side.sources, p.side.target, batch + (anchors or []))
        cols = []
        for x in batch:
            cols.append(f"SUM(CASE WHEN {dc._bucket_expr(x)} = 0 THEN 1 ELSE 0 END) AS e{x.i}")
            cols.append(f"SUM(CASE WHEN s.r{x.i} IS NOT NULL AND s.r{x.i} <> N'' THEN 1 ELSE 0 END) AS f{x.i}")
            # rows empty on both sides: identical, but they say nothing about which column is which
            cols.append(f"SUM(CASE WHEN s.r{x.i} IS NULL AND t.t{x.i} IS NULL THEN 1 ELSE 0 END) AS z{x.i}")
            cols.append(f"MIN(s.r{x.i}) AS lo{x.i}")
            cols.append(f"MAX(s.r{x.i}) AS hi{x.i}")
        sql = _paired_sql(p, side, key, anchors, sample, f"COUNT_BIG(*) AS paired, {', '.join(cols)}")
        r = _one(ctx, sql)
        for x in batch:
            out[x.i] = {"paired": int(r["paired"] or 0), "identical": int(r[f"e{x.i}"] or 0),
                        "filled": int(r[f"f{x.i}"] or 0), "both_empty": int(r[f"z{x.i}"] or 0),
                        "varied": r[f"lo{x.i}"] is not None and r[f"lo{x.i}"] != r[f"hi{x.i}"]}
    return out


def _paired_sql(p, side, key, anchors, sample, select, where_extra="", tail=""):
    """SELECT `select` FROM the paired rows (aliases s and t)."""
    if key is not None:
        src, tgt = side.source_sql(key_pair=key), side.target_sql(key_pair=key)
        k = (f", k AS (SELECT TOP ({int(sample)}) __k FROM s WHERE __k IS NOT NULL ORDER BY __k)" if sample else "")
        where = " AND ".join(x for x in ("s.__k IN (SELECT __k FROM k)" if sample else "", where_extra) if x)
        return (f"WITH s AS {src}, t AS {tgt}{k} SELECT {select} FROM s INNER JOIN t ON s.__k = t.__k"
                f"{' WHERE ' + where if where else ''} {tail}")
    from . import keymap
    src, tgt = side.source_sql(), side.target_sql()
    hs = keymap._hash([(f"x.c{a.i}", a) for a in anchors])
    ht = keymap._hash([(f"y.t{a.i}", a) for a in anchors])
    where = " AND ".join(x for x in ("s.__n = 1 AND t.__n = 1", where_extra) if x)
    return (f"""WITH s0 AS (SELECT x.*, {hs} AS __h FROM {src} x), t0 AS (SELECT y.*, {ht} AS __h FROM {tgt} y),
                 s AS (SELECT s0.*, COUNT_BIG(*) OVER (PARTITION BY s0.__h) AS __n FROM s0),
                 t AS (SELECT t0.*, COUNT_BIG(*) OVER (PARTITION BY t0.__h) AS __n FROM t0)
              SELECT {select} FROM s INNER JOIN t ON s.__h = t.__h WHERE {where} {tail}""")


def _examples(ctx, p, key, anchors, x, sample):
    """A few paired rows where the pair differs: the row, the source value, the target value."""
    side = dc.Side(p.side.sources, p.side.target, [x] + (anchors or []))
    label = "CONVERT(nvarchar(200), s.__k)" if key is not None else "CAST(NULL AS nvarchar(200))"
    sql = _paired_sql(p, side, key, anchors, sample,
                      f"TOP ({EXAMPLES}) {label} AS k, LEFT(s.r{x.i}, 120) AS sv, LEFT(t.u{x.i}, 120) AS tv",
                      where_extra=f"{dc._bucket_expr(x)} <> 0")
    return [{"row": r["k"], "source": r["sv"], "target": r["tv"]} for r in _run(ctx, sql)]


# ---- deciding ---------------------------------------------------------------------------------------

def _decide(m, p, key_label, key, anchors, named, cands, full, sample_res, examples_of, coverage, paired_rows):
    smaller = min(p.source_rows, p.target_rows)
    enough_rows = paired_rows >= MIN_COVERAGE * smaller if smaller else False

    def rate(r):
        """Share of the rows holding a value (on either side) that are identical: rows empty on
        both sides are left out, or an id that replaced text would look 60% right."""
        if not r:
            return 0.0
        with_value = r["paired"] - r.get("both_empty", 0)
        return (r["identical"] - r.get("both_empty", 0)) / with_value if with_value > 0 else 0.0

    def needed(r):
        """Filled values a proof needs: RENAME_MIN_VALUES, or half the paired rows of a smaller table."""
        return min(MIN_VALUES, max(1, r["paired"] // 2))

    def proven(r):
        return bool(r and r["paired"] and r["identical"] == r["paired"] and r["filled"] >= needed(r) and r["varied"])

    def why_not(r, sampled=False):
        why = []
        if r["identical"] < r["paired"]:
            with_value = r["paired"] - r.get("both_empty", 0)
            why.append(f"{_fmt(r['paired'] - r['identical'])} of the {_fmt(with_value)} "
                       f"{'sampled' if sampled else 'paired'} rows with a value differ")
        if r["filled"] < needed(r):
            why.append(f"only {_fmt(r['filled'])} rows hold a value (needs {_fmt(needed(r))})")
        if not r["varied"]:
            why.append("the column holds one value only, which proves nothing")
        return "; ".join(why) + "."

    # Every leftover pair that is 100% identical on all paired rows.
    perfect = [c for c in cands if proven(full.get(c.i))]
    by_src, by_tgt = {}, {}
    for c in perfect:
        by_src.setdefault(c.source["name"], []).append(c)
        by_tgt.setdefault(c.target["name"], []).append(c)

    decisions = []
    seen_src = set()
    for c in perfect:
        s, t = c.source["name"], c.target["name"]
        if s in seen_src:
            continue
        r = full[c.i]
        rivals = sorted({x.target["name"] for x in by_src[s]} | {x.source["name"] for x in by_tgt[t]} - {s, t})
        base = {"source": s, "target": t, "source_type": c.source["type"], "target_type": c.target["type"],
                "rows_checked": r["paired"], "identical": r["identical"], "differing": r["paired"] - r["identical"],
                "filled": r["filled"], "rate": 1.0, "full": True, "name_score": name_score(s, t), "match": None}
        if len(by_src[s]) > 1 or len(by_tgt[t]) > 1:
            others = sorted({x.target["name"] for x in by_src[s]} if len(by_src[s]) > 1
                            else {x.source["name"] for x in by_tgt[t]})
            decisions.append({**base, "verdict": "ambiguous", "also": others,
                              "reason": f"identical on all {_fmt(r['paired'])} paired rows, but so is "
                                        f"{', '.join(x for x in others if x not in (s, t))}: the data cannot tell "
                                        "which it is. Choose one in the mapping file (columns:)."})
            seen_src.add(s)
            continue
        if not enough_rows:
            decisions.append({**base, "verdict": "possible",
                              "reason": f"identical on all {_fmt(r['paired'])} paired rows, but only "
                                        f"{coverage * 100:.0f}% of the rows could be paired "
                                        f"(needs {MIN_COVERAGE * 100:.0f}%)."})
        else:
            decisions.append({**base, "verdict": "verified",
                              "reason": f"identical on all {_fmt(r['paired'])} paired rows."})
        seen_src.add(s)

    # The best near miss of every leftover source column that has none of the above - best
    # first, and each target column suggested once only (for its best match).
    used_t = {d["target"] for d in decisions}
    near = []
    for s in dict.fromkeys(c.source["name"] for c in cands):
        if s in seen_src:
            continue
        scored = [(c, full.get(c.i) or sample_res.get(c.i)) for c in cands if c.source["name"] == s]
        near += [(c, r) for c, r in scored if r and r["paired"] and rate(r) >= POSSIBLE_MIN]
    near.sort(key=lambda cr: (-rate(cr[1]), -name_score(cr[0].source["name"], cr[0].target["name"])))
    for c, r in near:
        if c.source["name"] in seen_src or c.target["name"] in used_t:
            continue
        seen_src.add(c.source["name"])
        used_t.add(c.target["name"])
        s = c.source["name"]
        is_full = c.i in full
        decisions.append({"source": s, "target": c.target["name"], "source_type": c.source["type"],
                          "target_type": c.target["type"], "rows_checked": r["paired"], "identical": r["identical"],
                          "differing": r["paired"] - r["identical"], "filled": r["filled"], "rate": round(rate(r), 6),
                          "full": is_full, "name_score": name_score(s, c.target["name"]), "match": None,
                          "verdict": "possible", "reason": why_not(r, sampled=not is_full),
                          "examples": examples_of.get(c.i, [])})

    # The pairs the names made: does the data agree?
    named_checks = []
    for x in named:
        r = full.get(x.i)
        if not r or not r["paired"]:      # no paired row: the data says nothing either way
            continue
        ok = proven(r)
        named_checks.append({"source": x.source["name"], "target": x.target["name"], "match": x.match,
                             "rows_checked": r["paired"], "identical": r["identical"],
                             "differing": r["paired"] - r["identical"], "rate": round(rate(r), 6),
                             # "not supported" only when rows really differ; identical but unproven
                             # (empty, one value, too few rows paired) stays possible
                             "verdict": ("verified" if ok and enough_rows
                                         else "not_supported" if r["identical"] < r["paired"] else "possible"),
                             "reason": (f"identical on all {_fmt(r['paired'])} paired rows."
                                        + ("" if enough_rows else f" But only {coverage * 100:.0f}% of the rows could be paired.")
                                        if ok else why_not(r)),
                             "examples": examples_of.get(x.i, [])})
    decisions.sort(key=lambda d: ({"verified": 0, "ambiguous": 1, "possible": 2}[d["verdict"]], d["source"].lower()))
    return decisions, named_checks


# ---- one mapping ------------------------------------------------------------------------------------

def check(m, entry_of, ctx=None):
    """The rename check of every target of mapping `m`."""
    ctx = ctx or Context()
    started = time.time()
    result = {"mapping": m.id, "type": m.type, "checked_at": _now(), "targets": []}
    try:
        for tmember in m.targets:
            result["targets"].append(_check_target(ctx, m, entry_of, tmember))
        verified = sum(1 for t in result["targets"] for d in t.get("decisions", []) if d["verdict"] == "verified")
        other = sum(1 for t in result["targets"] for d in t.get("decisions", []) if d["verdict"] != "verified")
        result.update(status="done", headline=f"{verified} rename{'s' if verified != 1 else ''} verified by the data"
                                              f"{f', {other} to review' if other else ''}.")
    except Skipped as exc:
        result.update(status="skipped", headline=str(exc))
    except (dc.Skipped, db.TableLocked) as exc:
        text = str(exc) if isinstance(exc, dc.Skipped) else "A table is locked by another session (a load in progress?)."
        result.update(status="locked", headline=text)
    except Stopped:
        raise
    except pyodbc.Error as exc:
        text = str(exc)
        result.update(status="timeout" if "query timeout" in text.lower() else "error",
                      headline=f"Check failed: {text}")
    result["seconds"] = round(time.time() - started, 1)
    return result


def _check_target(ctx, m, entry_of, tmember):
    p = _prepare(m, entry_of, tmember)
    out = {"target": tmember.ref.ref, "signature": signature(p), "source_rows": p.source_rows,
           "target_rows": p.target_rows, "decisions": [], "named": []}
    named_rows, cand_rows, left_s, left_t = _candidates(p)
    out["leftover"] = {"source": [c["name"] for c in left_s], "target": [c["name"] for c in left_t]}
    if not cand_rows and not named_rows:
        out.update(method=None, headline="No column is left to match: every column has a partner by name.")
        return out
    if len(cand_rows) > MAX_CANDIDATES:
        log.add(f"{_fmt(len(cand_rows))} possible pairs: only the first {_fmt(MAX_CANDIDATES)} (closest names first) "
                "are tried (RENAME_MAX_CANDIDATES).", "warn", mapping=m.id)
        cand_rows = cand_rows[:MAX_CANDIDATES]
    named = [dc.Pair(CAND_BASE + i, r) for i, r in enumerate(named_rows)]
    cands = [dc.Pair(CAND_BASE + len(named) + i, r) for i, r in enumerate(cand_rows)]
    log.add(f"{tmember.ref.ref}: {len(left_s)} source and {len(left_t)} target columns without a partner; "
            f"{_fmt(len(cands))} pairs can hold the same kind of value, {len(named)} name pairs to verify.",
            mapping=m.id)

    # 1. Pair the rows.
    key, how = _pick_key(ctx, m, p)
    anchors = None
    if key is None:
        log.add(f"No key to pair rows ({how}); pairing on the columns that hold the same values on both sides.",
                mapping=m.id, step=True)
        anchors = _anchors(ctx, p)
        if not anchors:
            out.update(method=None, headline="Cannot verify: no key, and no column holds the same values on both "
                                             "sides, so rows cannot be paired.",
                       unmatched=[{"source": s, "reason": "rows cannot be paired"} for s in out["leftover"]["source"]])
            return out
        how = f"{len(anchors)} columns with the same values on both sides"
    out.update(method="key" if key is not None else "columns", paired_on=how)
    log.add(f"Rows are paired on {how}.", mapping=m.id)

    # 2. On a big table, a sample rules out the pairs that differ at once.
    big = max(p.source_rows, p.target_rows) > FULL_BELOW and key is not None
    sample_res = {}
    survivors = cands
    if big:
        log.add(f"Trying {_fmt(len(cands))} pairs on {_fmt(SAMPLE_ROWS)} paired rows first (a differing row rules a "
                "pair out; the sample never confirms one).", mapping=m.id, step=True)
        sample_res = _measure(ctx, p, key, anchors, cands, SAMPLE_ROWS)
        if not any(r["paired"] for r in sample_res.values()):
            log.add("The sample paired no rows: every pair is checked in full instead.", "warn", mapping=m.id)
            survivors = cands
        else:
            survivors = [c for c in cands if sample_res[c.i]["paired"] and sample_res[c.i]["identical"] == sample_res[c.i]["paired"]]
        log.add(f"{len(survivors)} of {_fmt(len(cands))} pairs are identical on every sampled row.", mapping=m.id)

    # 3. Every pair still standing, and every name pair, on all paired rows.
    todo = survivors + named
    full = {}
    if todo:
        log.add(f"Comparing {len(todo)} pairs on every paired row"
                f"{f' (about {_fmt(min(p.source_rows, p.target_rows))} rows)' if big else ''}.", mapping=m.id, step=True)
        t0 = time.time()
        full = _measure(ctx, p, key, anchors, todo, None)
        log.add(f"Full comparison done in {_took(time.time() - t0)}.", mapping=m.id)
    # Paired rows are known from the full comparison; when every pair was ruled out by the
    # sample there was none, and the count is left unknown rather than guessed.
    paired_rows = max([r["paired"] for r in full.values()] or [0])
    measured = bool(full)
    smaller = min(p.source_rows, p.target_rows)
    coverage = paired_rows / smaller if smaller else 0.0
    out["rows"] = {"source": p.source_rows, "target": p.target_rows, "paired": paired_rows if measured else None,
                   "unpaired_source": max(0, p.source_rows - paired_rows) if measured else None,
                   "unpaired_target": max(0, p.target_rows - paired_rows) if measured else None,
                   "coverage": round(coverage, 4) if measured else None, "sampled": SAMPLE_ROWS if big else None}

    # 4. Examples for the near misses, and the name pairs the data does not support.
    examples_of = {}
    near = []
    for s in dict.fromkeys(c.source["name"] for c in cands):
        mine = [(c, full.get(c.i) or sample_res.get(c.i)) for c in cands if c.source["name"] == s]
        mine = [(c, r) for c, r in mine if r and r["paired"] and r["identical"] < r["paired"]
                and r["identical"] >= POSSIBLE_MIN * r["paired"]]
        if mine:
            near.append(max(mine, key=lambda cr: cr[1]["identical"] / cr[1]["paired"])[0])
    near += [x for x in named if full.get(x.i) and full[x.i]["identical"] < full[x.i]["paired"]]
    for x in near[:20]:
        examples_of[x.i] = _examples(ctx, p, key, anchors, x, SAMPLE_ROWS if big else None)

    out["decisions"], out["named"] = _decide(m, p, how, key, anchors, named, cands, full, sample_res, examples_of,
                                             coverage, paired_rows)
    decided = {d["source"] for d in out["decisions"]}
    out["unmatched"] = [{"source": s, "reason": "no target column holds the same values"}
                        for s in out["leftover"]["source"] if s not in decided]
    v = sum(1 for d in out["decisions"] if d["verdict"] == "verified")
    review = sum(1 for d in out["decisions"] if d["verdict"] != "verified")
    unmatched = len(out["unmatched"])
    parts = [f"{v} rename{'s' if v != 1 else ''} verified by the data"]
    if review:
        parts.append(f"{review} to review")
    if unmatched:
        parts.append(f"{unmatched} source column{'s' if unmatched != 1 else ''} with no matching target column")
    out["headline"] = "; ".join(parts) + "."
    return out


# ---- store and background run ------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.results = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.results = {}

    def get(self, mapping_id):
        return self.results.get(mapping_id)

    def put(self, result):
        """A failed attempt (locked, timeout, error) does not replace a finished result."""
        with self.lock:
            old = self.results.get(result["mapping"])
            if result["status"] in ("locked", "timeout", "error") and old and old.get("status") == "done":
                kept = dict(old)
                kept["last_attempt"] = {k: result.get(k) for k in ("status", "headline", "checked_at")}
                self.results[result["mapping"]] = kept
            else:
                self.results[result["mapping"]] = result
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.results, default=str), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass


store = Store(RESULT_FILE)
job = {"running": False, "cancelling": False, "done": 0, "total": 0, "current": None, "started_at": None,
       "finished_at": None, "error": None, "waiting": None}
_ctx = None
_job_lock = threading.Lock()


def _other_run():
    """A heavy run elsewhere that reads RDS too: the data check run, or the ATNM copy check."""
    try:
        from . import main as app_main
        if app_main._checks.job.get("running"):
            return "the RDS → AlTasnimBI data check run"
    except Exception:
        pass
    try:
        from .ATNM import jobs as atnm_jobs
        if atnm_jobs.job.get("running"):
            return "the ATNM copy check"
    except Exception:
        pass
    return None


def stale(result, mapping, entry_of):
    """True when the columns of any target changed since the result was made."""
    if not result or result.get("status") != "done":
        return False
    for t in result.get("targets", []):
        tm = next((x for x in mapping.targets if x.ref.ref == t["target"]), None)
        if tm is None:
            return True
        try:
            if signature(_prepare(mapping, entry_of, tm)) != t.get("signature"):
                return True
        except Exception:
            return False
    return False


def start(mappings, entry_of_factory, refresh=False):
    """Check `mappings` one after another in the background (with refresh=False the ones
    with an up-to-date result are left out). False when a run is going."""
    global _ctx
    with _job_lock:
        if job["running"]:
            return False
        _ctx = Context()
        ctx = _ctx
        job.update(running=True, cancelling=False, done=0, total=len(mappings), current=None, started_at=_now(),
                   finished_at=None, error=None, waiting=None)
    log.clear()
    log.add(f"Rename check started for {len(mappings)} mapping{'s' if len(mappings) != 1 else ''}.", step=True)

    def work():
        t0 = time.time()
        try:
            for m in mappings:
                if ctx.cancelled:
                    break
                other = _other_run()
                if other:
                    log.add(f"Waiting for {other} to finish: one heavy run on RDS at a time.", "warn", step=True)
                    job["waiting"] = other
                    while _other_run() and not ctx.stopped.wait(15):
                        pass
                    job["waiting"] = None
                    if ctx.cancelled:
                        break
                entry_of = entry_of_factory()
                saved = store.get(m.id)
                if not refresh and saved and saved.get("status") == "done" and not stale(saved, m, entry_of):
                    log.add("Up to date: left out.", mapping=m.id)
                    job["done"] += 1
                    continue
                job["current"] = m.id
                log.add(f"Checking mapping {m.id} ({m.type}).", mapping=m.id, step=True)
                try:
                    result = check(m, entry_of, ctx)
                except Stopped:
                    log.add("Stopped while checking this mapping; nothing saved for it.", "warn", mapping=m.id)
                    break
                store.put(result)
                job["done"] += 1
                log.add(f"{result['status']}: {result['headline']} ({result['seconds']} s)",
                        "ok" if result["status"] == "done" else "warn", mapping=m.id)
        except Exception as exc:
            job["error"] = str(exc)
            log.add(f"The run stopped: {exc}", "error")
        finally:
            took = _took(time.time() - t0)
            log.add(f"{'Stopped' if ctx.cancelled else 'Finished'} after {took}: {job['done']} of {job['total']} "
                    "mappings.", "warn" if ctx.cancelled else "ok", step=True)
            job.update(running=False, cancelling=False, current=None, finished_at=_now(), waiting=None)

    threading.Thread(target=work, daemon=True, name="rename-check").start()
    return True


def status(since=None):
    out = dict(job)
    out["step"], out["step_started_at"], out["log_seq"] = log.step, log.step_started_at, log.last
    if since is not None:
        out["log"] = log.since(since)
    return out


def cancel():
    with _job_lock:
        if not job["running"] or _ctx is None:
            return False
        job["cancelling"] = True
        log.add("Stop requested.", "warn", step=True)
        _ctx.cancel()
        return True


# ---- HTTP API -------------------------------------------------------------------------------------------

router = APIRouter(prefix="/api/renames", tags=["Renames"])


def _app():
    from . import main as app_main
    return app_main


def _entry_of_factory():
    app_main = _app()
    live = app_main._live_tables()
    return lambda member: app_main._entry(live, member.ref)


@router.get("")
def saved(mapping: str = Query(..., description="Mapping id, e.g. activity_task_plan")):
    """The last rename check of a mapping (null when none), and whether it is out of date."""
    m = _app()._mapping_by_id(mapping)
    r = store.get(m.id)
    out = dict(r) if r else None
    if out:
        out["stale"] = stale(r, m, _entry_of_factory())
    return {"result": out, "job": status()}


class RunRequest(BaseModel):
    mapping: str | None = None
    refresh: bool = False


@router.post("/run")
def run(req: RunRequest):
    """Check one mapping (always measured again) or every mapping (up-to-date ones left out
    unless refresh), in the background."""
    app_main = _app()
    if req.mapping:
        mappings, refresh = [app_main._mapping_by_id(req.mapping)], True
    else:
        mappings = [m for m in app_main.get_plan().mappings if m.type not in ("transform", "excluded")]
        refresh = req.refresh
    if not start(mappings, _entry_of_factory, refresh):
        raise HTTPException(status_code=409, detail="A rename check is already running.")
    return status()


@router.get("/status")
def run_status(since: int | None = Query(None, ge=0)):
    return status(since)


@router.post("/cancel")
def run_cancel():
    return {"cancelled": cancel(), "job": status()}
