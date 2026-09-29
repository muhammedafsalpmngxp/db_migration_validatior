"""Key mapping check: does every foreign key of the new table point at the right row?

The new tables often store a number where the old table stored text: New_Crew_code
'YLM-0401' became crew_type_id 395, which is row 395 of ref.crew_type. For every
single-column foreign key of a mapping's target table this module works out

  1. the source column that feeds it: the column the column pairing gave it, or else the
     source text column whose values are found in the referenced table (measured);
  2. the referenced column that holds what the source stored (crew_type_code, uom_code), or
     the key itself when the value was copied as it is (a code). A number copied from an
     old list that was migrated too is compared through a label both lists share: old
     EquipmentType 396 'MDG01' must be new equipment_type 396 'MDG01';
  3. row by row: the target id is turned back into that value and compared with the source
     value of the same row. Rows are paired on the data check's key; a table without one is
     paired on its other columns that hold the same values on both sides, and the rows that
     still cannot be paired are compared by value counts;
  4. when the target also keeps the value itself (new_crew_code next to crew_type_id),
     whether the two agree on every target row.

Each paired row lands in exactly one bucket:
    correct      the id points at the source value
    case_only    the same value written differently ('No' -> 'no'): the list keeps one spelling
    both_empty   no value on either side
    not_filled   the source value is in the list, but the id is empty
    not_in_list  the source value is not in the list, so there is no id to give
    wrong        the id points at a different value
    added        an id where the source is empty
    orphan       the id is not in the list (possible when the constraint is disabled or untrusted)
    old_broken   (old list compare) the old value points at no row of the old list

Read-only and READ UNCOMMITTED from the target connection, like the data check, whose
column pairing, type conversion and key choice are reused so the two always agree.
"""
import difflib
import re
import time
from datetime import datetime, timezone

import pyodbc

from . import compare, config, db
from . import datacheck as dc
from . import mapping as plan_mod
from .values import HIDDEN, is_sensitive

TARGET = config.TARGET_SIDE
TEXT, NOCOMPARE, DATES = dc.TEXT, dc.NOCOMPARE, dc.DATES
CI, BIN, NULL_MARK = dc.CI, dc.BIN, dc.NULL_MARK
NUMERIC = {"int", "bigint", "smallint", "tinyint", "numeric", "decimal"}
_q, _full, _bt, _raw_text, _n, _fmt = dc._q, dc._full, dc._bt, dc._raw_text, dc._n, dc._fmt

BUCKETS = ("correct", "case_only", "both_empty", "not_filled", "not_in_list", "wrong", "added", "orphan",
           "old_broken")
B = {name: i for i, name in enumerate(BUCKETS)}
PROBLEM_BUCKETS = ("not_filled", "not_in_list", "wrong", "added", "orphan", "old_broken")
AGREEMENT = ("agree", "case_only", "both_empty", "code_not_linked", "code_not_in_list", "code_empty", "disagree",
             "orphan")
A = {name: i for i, name in enumerate(AGREEMENT)}
DISCOVERY_SAMPLE = 50_000    # rows per source table read when looking for the column that feeds a key
CODE_LENGTH = 200            # longer values are never codes, so the search skips them
GROUPS = 8                   # example values kept per bucket
MAX_PAGE_SIZE = 200
# Anchor columns that read well as a row label when a table has no key.
LABELISH = re.compile(r"(id|no|num|number|code|name)$", re.IGNORECASE)


class NotReady(Exception):
    """The rows cannot be shown yet; the message says why."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _txt(expr, type_name):
    """A value as trimmed text for comparing (case-insensitive collation); blank is empty."""
    return f"NULLIF({_raw_text(expr, type_name)}, N'')"


def _col(c):
    return c and {"name": c["name"], "type": c["type"]}


class Link:
    """One foreign key column of the target and everything needed to check it."""

    def __init__(self, j, fk, fcol, kcol, ref_cols):
        self.j = j                      # index: column names in SQL are suffixed with it
        self.fk = fk
        self.f = fcol                   # the target's foreign key column
        self.k = kcol                   # the referenced key column
        self.ref = fk["referenced"]     # {schema, table, rows}
        self.ref_cols = ref_cols
        self.s = None                   # the source column that feeds it
        self.how = None                 # how it was chosen: a pairing round, or "found" (by its values)
        self.r = None                   # the referenced column holding what the source stored
        self.mode = None                # lookup | code | meaning
        self.copy = None                # a target column that keeps the source value as well
        self.coverage = None            # {"distinct", "found", "sampled"}
        self.old = None                 # meaning: the old list and its key and label columns
        self.reason = None              # why it cannot be checked

    @property
    def ref_name(self):
        return f"{self.ref['schema']}.{self.ref['table']}"

    @property
    def label(self):
        return f"{self.s['name']} → {self.f['name']}" if self.s else self.f["name"]

    @property
    def via(self):
        """Where the target id is looked up, in words."""
        if self.mode == "meaning":
            o = self.old
            return f"old {o['schema']}.{o['table']}.{o['label']} = {self.ref_name}.{self.r['name']}"
        return f"{self.ref_name}.{self.r['name']}" if self.r else self.ref_name

    def spec(self):
        return {"fk": self.fk["name"], "column": _col(self.f), "key": _col(self.k),
                "ref": {"schema": self.ref["schema"], "table": self.ref["table"]},
                "source": _col(self.s), "label": _col(self.r), "mode": self.mode, "copy": _col(self.copy),
                "old": self.old}

    @classmethod
    def from_spec(cls, spec, j=0):
        link = cls(j, {"name": spec["fk"], "referenced": spec["ref"]}, spec["column"], spec["key"], [])
        link.s, link.r, link.mode, link.copy, link.old = (spec["source"], spec["label"], spec["mode"],
                                                          spec["copy"], spec["old"])
        return link


# ---- which columns take part ----------------------------------------------------------------

def _ref_columns(ref):
    """Columns of a referenced table, or None when it cannot be read (missing or locked)."""
    try:
        oid = db.query(TARGET, "SELECT OBJECT_ID(?) AS o", (f"{_q(ref['schema'])}.{_q(ref['table'])}",))[0]["o"]
        return db.table_columns(TARGET, oid) if oid else None
    except db.TableLocked:
        return None
    except pyodbc.Error as exc:
        if db.is_lock_timeout(exc):
            return None
        raise


def _links(prep):
    """One Link per single-column foreign key of the target, with the source column the
    column pairing gave it (if any)."""
    by_target = {r["target"]["name"].lower(): r for r in prep.rows if r["source"] and r["target"]}
    refs, links = {}, []
    for name, fk in prep.outgoing.items():
        fcol = next((c for c in prep.tcols if c["name"].lower() == name), None)
        if not fcol:
            continue
        ref = fk["referenced"]
        rk = (ref["schema"].lower(), ref["table"].lower())
        if rk not in refs:
            refs[rk] = _ref_columns(ref)
        ref_cols = refs[rk]
        kcol = next((c for c in ref_cols or [] if c["name"].lower() == fk["ref_columns"][0].lower()), None)
        link = Link(len(links), fk, fcol, kcol, ref_cols or [])
        if ref_cols is None or kcol is None:
            link.reason = f"{link.ref_name} cannot be read right now (missing or locked by a load)."
        row = by_target.get(name)
        if row and _bt(row["source"]["type"]) not in NOCOMPARE:
            link.s, link.how = row["source"], row["match"]
        links.append(link)
    return links


def _candidates(link, searching):
    """Referenced columns that may hold what the source stored. A paired source column may
    match any comparable column, the key included (a code copied as it is). A search looks
    only at text columns other than the key, so a column of small numbers cannot match ids
    by chance."""
    out = []
    for c in link.ref_cols:
        if _bt(c["type"]) in NOCOMPARE:
            continue
        is_key = c["name"].lower() == link.k["name"].lower()
        if not searching or (_bt(c["type"]) in TEXT and not is_key):
            out.append(c)
    return out


def _find_sources(prep, links):
    """For each link: which referenced column holds its source values, and - with no paired
    source column - which source column feeds it.

    One read-only query per mapping over at most DISCOVERY_SAMPLE rows of each source
    table: the distinct values of every candidate source column, and how many of them are
    found in each candidate column of each referenced table. The row check that follows is
    exact; this only decides what to compare.
    """
    todo = [lk for lk in links if not lk.reason]
    if not todo:
        return
    scols = [c for c in prep.source_cols[0] if _bt(c["type"]) in TEXT]
    for lk in todo:
        if lk.s is not None and all(c["name"].lower() != lk.s["name"].lower() for c in scols):
            scols.append(lk.s)
    tests = [(lk, c) for lk in todo for c in _candidates(lk, searching=lk.s is None)]
    sampled = any(e["rows"] > DISCOVERY_SAMPLE for e in prep.sentries)
    hits, total = {}, {}
    if scols and tests:
        branches = []
        for member, entry, cols in prep.side.sources:
            parts = []
            for n, c in enumerate(scols):
                col = cols.get(c["name"].lower())
                parts.append(f"{_raw_text('x.' + _q(col['name']), col['type'])} AS s{n}" if col
                             else f"CAST(NULL AS nvarchar(4000)) AS s{n}")
            branches.append(f"SELECT * FROM (SELECT TOP ({DISCOVERY_SAMPLE}) {', '.join(parts)} "
                            f"FROM {_full(member.ref.side, entry['schema'], entry['table'])} x) b{len(branches)}")
        values = ", ".join(f"({n}, x.s{n})" for n in range(len(scols)))
        # Values are cast to nvarchar(CODE_LENGTH) once they are known to fit: sorting them as
        # nvarchar(4000) is ten times slower.
        short = f"CAST(v AS nvarchar({CODE_LENGTH})) COLLATE {CI}"
        labels = " UNION ALL ".join(
            f"SELECT DISTINCT {t} AS j, {short} AS v FROM (SELECT {_raw_text('r.' + _q(c['name']), c['type'])} AS v "
            f"FROM {_full(TARGET, lk.ref['schema'], lk.ref['table'])} r) q{t} WHERE LEN(v) <= {CODE_LENGTH}"
            for t, (lk, c) in enumerate(tests))
        rows = dc._run(f"""
            WITH x AS ({' UNION ALL '.join(branches)}),
                 sv AS (SELECT DISTINCT c.n, CAST(c.v AS nvarchar({CODE_LENGTH})) COLLATE {CI} AS v
                        FROM x CROSS APPLY (VALUES {values}) c(n, v)
                        WHERE c.v IS NOT NULL AND c.v <> N'' AND LEN(c.v) <= {CODE_LENGTH}),
                 lv AS ({labels})
            SELECT sv.n, lv.j, COUNT_BIG(*) AS hits FROM sv INNER HASH JOIN lv ON lv.v = sv.v GROUP BY sv.n, lv.j
            UNION ALL
            SELECT n, -1, COUNT_BIG(*) FROM sv GROUP BY n""")
        total = {r["n"]: _n(r["hits"]) for r in rows if r["j"] == -1}
        hits = {(r["n"], r["j"]): _n(r["hits"]) for r in rows if r["j"] >= 0}
    index = {c["name"].lower(): n for n, c in enumerate(scols)}
    per_link = {}
    for t, (lk, c) in enumerate(tests):
        per_link.setdefault(lk.j, []).append((t, c))

    # A paired source column: the referenced column that holds most of its values (the key on a tie).
    for lk in todo:
        if lk.s is None:
            continue
        n = index[lk.s["name"].lower()]
        options = per_link.get(lk.j, [])
        best = max(options, key=lambda tc: (hits.get((n, tc[0]), 0), tc[1]["name"].lower() == lk.k["name"].lower()),
                   default=None)
        found, distinct = (hits.get((n, best[0]), 0) if best else 0), total.get(n, 0)
        lk.coverage = {"distinct": distinct, "found": found, "sampled": sampled}
        if best and found:
            lk.r = best[1]
        elif best and not distinct:
            # The source column is empty: every row must then have no id. Compare through the
            # first text column (or the key) so an id that appears anyway can be shown.
            lk.r = next((c for _, c in options if _bt(c["type"]) in TEXT and c is not lk.k), lk.k)
        else:
            lk.reason = (f"None of the {_fmt(distinct)} different values of {lk.s['name']} is found in any column of "
                         f"{lk.ref_name}, so this link cannot be checked.")

    # No paired source column: the source text column whose values are found in a text column
    # of the referenced table - most values found first, then the share found, then the name.
    options = []
    for lk in todo:
        if lk.s is not None:
            continue
        need = 1 if lk.ref["rows"] <= 2 else 2
        for t, c in per_link.get(lk.j, []):
            for n, sc in enumerate(scols):
                h, tot = hits.get((n, t), 0), total.get(n, 0)
                if _bt(sc["type"]) not in TEXT or not tot or h < need or h < 0.5 * tot:
                    continue
                sim = difflib.SequenceMatcher(None, compare.normalize(sc["name"]),
                                              compare.normalize(lk.f["name"])).ratio()
                options.append(((h, h / tot, sim), lk, sc, c, tot))
    options.sort(key=lambda o: o[0], reverse=True)
    taken = set()
    for score, lk, sc, c, tot in options:
        # Two keys to the same list (from_x_id, to_x_id) are not fed by the same source column.
        claim = (sc["name"].lower(), lk.ref["schema"].lower(), lk.ref["table"].lower())
        if lk.s is not None or claim in taken:
            continue
        lk.s, lk.how, lk.r = sc, "found", c
        lk.coverage = {"distinct": tot, "found": score[0], "sampled": sampled}
        taken.add(claim)
    for lk in todo:
        if lk.s is None:
            lk.reason = (f"No source column holds values that are found in {lk.ref_name}, so it is not known what "
                         "this link was made from.")
        elif not lk.reason and lk.r is not None:
            lk.mode = "code" if lk.r["name"].lower() == lk.k["name"].lower() else "lookup"


def _old_list(prep, link, plan, entry_of):
    """A number copied as it is, into a list that was migrated from an old list too: the old
    list, the old column the numbers point at, and a label both lists share. None when that
    cannot be established - the value is then compared as it is."""
    if (_bt(link.k["type"]) not in NUMERIC or _bt(link.s["type"]) not in NUMERIC | TEXT
            or len(prep.side.sources) != 1):
        return None
    ref_ref = plan_mod.parse_ref(f"{TARGET}.{link.ref['schema']}.{link.ref['table']}")
    rm = plan.by_target.get(ref_ref.key)
    if not rm or rm.type not in ("one_to_one", "merge"):
        return None
    old = next((s for s in rm.sources if s.role == "driving"), rm.sources[0])
    entry = entry_of(old)
    if not entry or entry["locked"]:
        return None
    try:
        old_cols = db.table_columns(old.ref.side, entry["object_id"])
    except db.TableLocked:
        return None
    old_full = _full(old.ref.side, entry["schema"], entry["table"])
    member, sentry, cols = prep.side.sources[0]
    col = cols.get(link.s["name"].lower())
    if not col:
        return None
    child = _full(member.ref.side, sentry["schema"], sentry["table"])
    sexpr = f"TRY_CONVERT(bigint, x.{_q(col['name'])})"
    head = f"WITH v AS (SELECT DISTINCT {sexpr} AS v FROM {child} x WHERE {sexpr} IS NOT NULL)"

    # The old column the numbers point at: unique, and holding most of the numbers used. Old
    # lists often keep ids as text ('396'), so text columns count too - compared as numbers.
    keys = [c for c in old_cols if _bt(c["type"]) in NUMERIC | TEXT]
    keys = sorted(keys, key=lambda c: (not compare.normalize(c["name"]).endswith("id"), _bt(c["type"]) not in NUMERIC))[:8]
    if not keys:
        return None
    parts = []
    for k, c in enumerate(keys):
        e = f"TRY_CONVERT(bigint, o.{_q(c['name'])})"
        parts.append(f"(SELECT COUNT_BIG({e}) FROM {old_full} o) AS n{k}, "
                     f"(SELECT COUNT_BIG(DISTINCT {e}) FROM {old_full} o) AS d{k}, "
                     f"(SELECT COUNT_BIG(*) FROM v WHERE v.v IN (SELECT {e} FROM {old_full} o)) AS h{k}")
    r = dc._one(f"{head} SELECT (SELECT COUNT_BIG(*) FROM v) AS total, {', '.join(parts)}")
    used = _n(r["total"])
    fits = [(k, _n(r[f"h{k}"])) for k in range(len(keys))
            if _n(r[f"n{k}"]) and _n(r[f"n{k}"]) == _n(r[f"d{k}"]) and _n(r[f"h{k}"]) >= max(1, 0.5 * used)]
    if not fits:
        return None
    okey = keys[max(fits, key=lambda x: x[1])[0]]

    # A label both lists share: a text pair of the list's own mapping, distinctive in the old
    # list, preferring the one on which the most used numbers agree.
    rows, _ = compare.align_columns(old_cols, link.ref_cols, target_table=link.ref["table"], declared=rm.columns)
    pairs = [(x["source"], x["target"]) for x in rows if x["source"] and x["target"]
             and _bt(x["source"]["type"]) in TEXT and _bt(x["target"]["type"]) in TEXT]
    pref = lambda p: (0 if "code" in p[1]["name"].lower() else 1 if "name" in p[1]["name"].lower() else 2)
    pairs = sorted(pairs, key=pref)[:8]
    if not pairs:
        return None
    tlist = _full(TARGET, link.ref["schema"], link.ref["table"])
    parts = []
    for k, (ls, lt) in enumerate(pairs):
        le, te = _txt(f"o.{_q(ls['name'])}", ls["type"]), _txt(f"t.{_q(lt['name'])}", lt["type"])
        parts.append(f"(SELECT COUNT_BIG({le}) FROM {old_full} o) AS f{k}, "
                     f"(SELECT COUNT_BIG(DISTINCT {le}) FROM {old_full} o) AS u{k}, "
                     f"(SELECT COUNT_BIG(*) FROM v JOIN {old_full} o ON TRY_CONVERT(bigint, o.{_q(okey['name'])}) = v.v "
                     f"JOIN {tlist} t ON TRY_CONVERT(bigint, t.{_q(link.k['name'])}) = v.v WHERE {le} = {te}) AS a{k}")
    r = dc._one(f"{head} SELECT {', '.join(parts)}")
    good = [k for k in range(len(pairs))
            if _n(r[f"f{k}"]) >= 0.5 * max(1, entry["rows"]) and _n(r[f"u{k}"]) >= 0.9 * _n(r[f"f{k}"])]
    if not good:
        return None
    k = max(good, key=lambda k: (_n(r[f"a{k}"]), -pref(pairs[k])))
    ls, lt = pairs[k]
    link.r = lt
    return {"side": old.ref.side, "schema": entry["schema"], "table": entry["table"],
            "key": okey["name"], "label": ls["name"], "label_type": ls["type"]}


def _copy_columns(prep, links):
    """A target text column that the same source column was copied into (new_crew_code next
    to crew_type_id): the two must agree on every target row."""
    by_source = {r["source"]["name"].lower(): r["target"] for r in prep.rows if r["source"] and r["target"]}
    fks = {lk.f["name"].lower() for lk in links}
    for lk in links:
        if lk.mode not in ("lookup", "code") or lk.s is None:
            continue
        t = by_source.get(lk.s["name"].lower())
        if t and t["name"].lower() not in fks and _bt(t["type"]) in TEXT:
            lk.copy = t


def _key(prep):
    """The row key, chosen exactly as the data check chooses it."""
    cands = [p for p in prep.pairs
             if not (p.target["name"].lower() in prep.fk_cols and _bt(p.stype) in TEXT and _bt(p.ttype) not in TEXT)]
    pk = [c.lower() for c in prep.pk["columns"]] if prep.pk else None
    return dc._pick_key(prep.side, cands, pk, prep.fk_cols)


MAX_ANCHORS = 200   # 32 bytes per column hash must fit in varbinary(8000)


def _norm(expr, pair):
    """One anchor value, forgiving of upper/lower case, outer spaces and blank vs NULL.
    (nvarchar(4000), not max: large-object text makes every row slow to hash.)"""
    style = ", 126" if not pair.text_compare and _bt(pair.ttype) in DATES else ""
    return f"ISNULL(UPPER(NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(4000), {expr}{style}))), N'')), N'{NULL_MARK}')"


def _hash(items):
    """One hash of several anchor values: each value hashed on its own, then the hashes."""
    if not items:
        return "CAST(0x00 AS varbinary(32))"
    parts = " + ".join(f"CAST(HASHBYTES('SHA2_256', {_norm(e, p)}) AS varbinary(32))" for e, p in items)
    return f"HASHBYTES('SHA2_256', {parts})"


def _anchors(prep, links):
    """For a table with no key: the paired columns that hold the same values on both sides.
    Rows are paired on them. A column that changed only lowers how many rows can be paired;
    it never pairs the wrong rows, because a pair is only made when its values are unique on
    both sides. The links' own columns never take part.

    With equal row counts a column must hold the same values the same number of times (one
    sum per column); otherwise the same set of distinct values, so missing rows do not rule
    out every column."""
    skip = {lk.f["name"].lower() for lk in links}
    cands = [p for p in prep.pairs if p.target["name"].lower() not in skip
             and not (_bt(p.stype) in TEXT and _bt(p.ttype) not in TEXT)]
    if not cands:
        return []
    def h(expr, p):
        return f"CAST(CAST(SUBSTRING(HASHBYTES('SHA2_256', {_norm(expr, p)}), 1, 4) AS int) AS bigint)"

    def measure(distinct):
        def sums(alias, col):
            if not distinct:
                return ", ".join(f"SUM({h(f'{alias}.{col}{p.i}', p)}) AS a{p.i}" for p in cands)
            return ", ".join(f"SUM(DISTINCT {h(f'{alias}.{col}{p.i}', p)}) AS a{p.i}, "
                             f"COUNT(DISTINCT {h(f'{alias}.{col}{p.i}', p)}) AS n{p.i}" for p in cands)
        return (dc._one(f"SELECT COUNT_BIG(*) AS __rows, {sums('x', 'c')} FROM {prep.side.source_sql()} x"),
                dc._one(f"SELECT COUNT_BIG(*) AS __rows, {sums('y', 't')} FROM {prep.side.target_sql()} y"))

    same_rows = sum(e["rows"] for e in prep.sentries) == prep.tentry["rows"]
    s, t = measure(distinct=not same_rows)
    if same_rows and s["__rows"] != t["__rows"]:   # the counts changed since the table list was read
        s, t = measure(distinct=True)
    return [p for p in cands
            if s[f"a{p.i}"] == t[f"a{p.i}"] and s.get(f"n{p.i}") == t.get(f"n{p.i}")][:MAX_ANCHORS]


def _label_pairs(anchors):
    """Up to two anchor columns that name a row for the examples (never contact details)."""
    ok = [a for a in anchors if not is_sensitive(a.source["name"], a.target["name"])]
    good = [a for a in ok if LABELISH.search(a.source["name"])]
    return (good + [a for a in ok if a not in good])[:2]


# ---- the row-level relation -------------------------------------------------------------------

def _src_expr(p, cols):
    col = cols.get(p.source["name"].lower())
    if not col:
        return f"CAST(NULL AS {'nvarchar(max)' if p.text_compare else p.ttype})"
    return p.source_value("x." + _q(col["name"]), col["type"])


def _source_rel(prep, key, links, anchors, labels):
    """The source tables stacked: __k (keyed) or __h / __lbl (keyless), and per link j:
    sr<j> the source value as stored, sv<j> the value to compare, sf<j> 0 when an old id
    points at no row of the old list."""
    branches = []
    for member, entry, cols in prep.side.sources:
        parts = ["N'" + entry["table"].replace("'", "''") + "' AS __src", "1 AS __s"]
        joins = []
        if key is not None:
            parts.append(f"{_src_expr(key, cols)} AS __k")
        else:
            parts.append(f"{_hash([(_src_expr(a, cols), a) for a in anchors])} AS __h")
            shown = [_raw_text("x." + _q(c["name"]), c["type"])
                     for c in (cols.get(a.source["name"].lower()) for a in labels) if c]
            parts.append(f"LEFT(CONCAT_WS(N' · ', {', '.join(shown)}), 200) AS __lbl" if len(shown) > 1
                         else f"LEFT({shown[0]}, 200) AS __lbl" if shown else "CAST(NULL AS nvarchar(200)) AS __lbl")
        for lk in links:
            col = cols.get(lk.s["name"].lower())
            if not col:
                parts += [f"CAST(NULL AS nvarchar(4000)) AS sr{lk.j}", f"CAST(NULL AS nvarchar(4000)) AS sv{lk.j}",
                          f"1 AS sf{lk.j}"]
                continue
            expr = "x." + _q(col["name"])
            sr = _txt(expr, col["type"])
            sv, sf = sr, "1"
            if lk.mode == "meaning":
                o, a = lk.old, f"o{lk.j}"
                # Both sides as numbers, exactly as the old key column was chosen (unique that way).
                joins.append(f"LEFT JOIN {_full(o['side'], o['schema'], o['table'])} {a} "
                             f"ON TRY_CONVERT(bigint, {a}.{_q(o['key'])}) = TRY_CONVERT(bigint, {expr})")
                sv = _txt(f"{a}.{_q(o['label'])}", o["label_type"])
                sf = f"CASE WHEN {a}.{_q(o['key'])} IS NULL THEN 0 ELSE 1 END"
            elif lk.mode == "code" and _bt(lk.k["type"]) not in TEXT and _bt(col["type"]) in TEXT:
                # A code kept as text in the source and as a number in the list: '3010' -> 3010.
                ktype = lk.k["type"]
                sv = f"COALESCE({_txt(f'TRY_CONVERT({ktype}, {expr})', ktype)}, {sr})"
            parts += [f"{sr} AS sr{lk.j}", f"{sv} AS sv{lk.j}", f"{sf} AS sf{lk.j}"]
        branches.append(f"SELECT {', '.join(parts)} FROM {_full(member.ref.side, entry['schema'], entry['table'])} x "
                        + " ".join(joins))
    return "(" + " UNION ALL ".join(branches) + ")"


def _target_rel(prep, key, links, anchors):
    """The target table: __k or __h, and per link j: tid<j> the id as text, tv<j> the value
    it points at, tf<j> 0 when the id is not in the list, tc<j> the copy column if any."""
    parts, joins = ["1 AS __t"], []
    if key is not None:
        parts.append(f"{key.target_value('y')} AS __k")
    elif anchors is not None:
        parts.append(f"{_hash([(a.target_value('y'), a) for a in anchors])} AS __h")
    for lk in links:
        r, f = f"r{lk.j}", "y." + _q(lk.f["name"])
        joins.append(f"LEFT JOIN {_full(TARGET, lk.ref['schema'], lk.ref['table'])} {r} ON {r}.{_q(lk.k['name'])} = {f}")
        parts.append(f"{_txt(f, lk.f['type'])} AS tid{lk.j}")
        parts.append(f"{_txt(r + '.' + _q(lk.r['name']), lk.r['type'])} AS tv{lk.j}")
        parts.append(f"CASE WHEN {f} IS NULL THEN NULL WHEN {r}.{_q(lk.k['name'])} IS NULL THEN 0 ELSE 1 END AS tf{lk.j}")
        parts.append(f"{_txt('y.' + _q(lk.copy['name']), lk.copy['type'])} AS tc{lk.j}" if lk.copy
                     else f"CAST(NULL AS nvarchar(4000)) AS tc{lk.j}")
    t = prep.tentry
    return f"(SELECT {', '.join(parts)} FROM {_full(TARGET, t['schema'], t['table'])} y {' '.join(joins)})"


def _list_values(lk, alias, against):
    """LEFT JOIN of the list's distinct values, to tell 'not filled' from 'not in the list'."""
    return (f"LEFT OUTER HASH JOIN (SELECT DISTINCT {_txt('r.' + _q(lk.r['name']), lk.r['type'])} AS v "
            f"FROM {_full(TARGET, lk.ref['schema'], lk.ref['table'])} r) {alias} ON {alias}.v = {against}")


def _bucket(lk):
    j = lk.j
    old = f"WHEN sr{j} IS NOT NULL AND sf{j} = 0 THEN {B['old_broken']} " if lk.mode == "meaning" else ""
    return (f"CASE WHEN sp IS NULL OR tp IS NULL THEN NULL {old}"
            f"WHEN sv{j} IS NULL AND tid{j} IS NULL THEN {B['both_empty']} "
            f"WHEN tid{j} IS NULL THEN CASE WHEN l{j}.v IS NULL THEN {B['not_in_list']} ELSE {B['not_filled']} END "
            f"WHEN tf{j} = 0 THEN {B['orphan']} "
            f"WHEN sv{j} IS NULL THEN {B['added']} "
            f"WHEN tv{j} COLLATE {BIN} = sv{j} COLLATE {BIN} THEN {B['correct']} "
            f"WHEN tv{j} = sv{j} THEN {B['case_only']} "
            f"ELSE {B['wrong']} END")


def _ctes(prep, key, links, anchors, labels):
    """(head, full): `head` defines s, t and m (the paired rows); `full` adds z, where every
    row carries per link j: sv/sr (source), tid/tv (target id and the value it points at) and
    b<j>, its bucket. Keyed: s FULL JOIN t on the key. Keyless: rows whose anchor values are
    unique on both sides are paired (u); the rest are left for the value-count comparison."""
    src = _source_rel(prep, key, links, anchors, labels)
    tgt = _target_rel(prep, key, links, None if key is not None else anchors)
    cols = ", ".join(f"s.sv{lk.j}, s.sr{lk.j}, s.sf{lk.j}, t.tid{lk.j}, t.tv{lk.j}, t.tf{lk.j}" for lk in links)
    if key is not None:
        head = f"""WITH s AS {src}, t AS {tgt},
            m AS (SELECT s.__s AS sp, t.__t AS tp, COALESCE(s.__k, t.__k) AS sk, {cols}
                  FROM s FULL OUTER HASH JOIN t ON s.__k = t.__k)"""
    else:
        # __n: how many rows share the anchor hash, counted in one pass over each side.
        head = f"""WITH s0 AS {src}, t0 AS {tgt},
            s AS (SELECT s0.*, COUNT_BIG(*) OVER (PARTITION BY s0.__h) AS __n FROM s0),
            t AS (SELECT t0.*, COUNT_BIG(*) OVER (PARTITION BY t0.__h) AS __n FROM t0),
            u AS (SELECT s.__h FROM s INNER HASH JOIN t ON t.__h = s.__h WHERE s.__n = 1 AND t.__n = 1),
            m AS (SELECT s.__s AS sp, t.__t AS tp, s.__lbl AS sk, {cols}
                  FROM s INNER HASH JOIN t ON t.__h = s.__h WHERE s.__n = 1 AND t.__n = 1)"""
    lists = " ".join(_list_values(lk, f"l{lk.j}", f"m.sv{lk.j}") for lk in links)
    buckets = ", ".join(f"{_bucket(lk)} AS b{lk.j}" for lk in links)
    return head, f"{head}, z AS (SELECT m.*, {buckets} FROM m {lists})"


# ---- measuring ----------------------------------------------------------------------------------

def _groups(prep, key, lk, anchors, labels):
    """The most common values of every bucket that needs a look, with row counts."""
    _, full = _ctes(prep, key, [lk], anchors, labels)   # this link only: cheaper than all of them
    j = lk.j
    shown = ", ".join(str(B[b]) for b in ("case_only",) + PROBLEM_BUCKETS)
    s_expr = f"LEFT(CASE WHEN b{j} = {B['old_broken']} THEN sr{j} ELSE sv{j} END, 120)"
    i_expr = f"LEFT(CASE WHEN b{j} IN ({B['wrong']}, {B['added']}, {B['orphan']}, {B['case_only']}) THEN tid{j} END, 60)"
    t_expr = f"LEFT(CASE WHEN b{j} IN ({B['wrong']}, {B['added']}, {B['case_only']}) THEN tv{j} END, 120)"
    grouped = [f"b{j}", s_expr, i_expr, t_expr]
    if lk.mode == "meaning":
        o_expr = f"LEFT(sr{j}, 60)"
        grouped.append(o_expr)
    else:
        o_expr = "CAST(NULL AS nvarchar(60))"   # a constant cannot be grouped on
    rows = dc._run(f"""{full}
        SELECT b, s, o, i, t, n, d FROM (
          SELECT b{j} AS b, {s_expr} AS s, {o_expr} AS o, {i_expr} AS i, {t_expr} AS t, COUNT_BIG(*) AS n,
                 ROW_NUMBER() OVER (PARTITION BY b{j} ORDER BY COUNT_BIG(*) DESC) AS rn,
                 COUNT(*) OVER (PARTITION BY b{j}) AS d
          FROM z WHERE b{j} IN ({shown})
          GROUP BY {', '.join(grouped)}) q
        WHERE rn <= {GROUPS} ORDER BY b, n DESC""")
    groups, distinct = {}, {}
    for r in rows:
        name = BUCKETS[r["b"]]
        groups.setdefault(name, []).append({"source": r["s"], "old": r["o"], "target_id": r["i"],
                                            "target_value": r["t"], "rows": _n(r["n"])})
        distinct[name] = _n(r["d"])
    return groups, distinct


def _remainder(prep, lk, anchors, labels):
    """Keyless rows that could not be paired one to one: compared by how many rows hold each
    value (the source value against the value the target id points at)."""
    head, _ = _ctes(prep, None, [lk], anchors, labels)
    j = lk.j
    tval = (f"CASE WHEN tid{j} IS NULL THEN NULL WHEN tf{j} = 0 THEN N'(id not in the list)' "
            f"ELSE ISNULL(tv{j}, N'(empty in the list)') END")
    base = f"""{head},
        rs AS (SELECT s.* FROM s LEFT OUTER HASH JOIN u ON u.__h = s.__h WHERE u.__h IS NULL),
        rt AS (SELECT t.* FROM t LEFT OUTER HASH JOIN u ON u.__h = t.__h WHERE u.__h IS NULL),
        a AS (SELECT sv{j} AS v, COUNT_BIG(*) AS n FROM rs GROUP BY sv{j}),
        b AS (SELECT {tval} AS v, COUNT_BIG(*) AS n FROM rt GROUP BY {tval}),
        d AS (SELECT a.v AS sv, b.v AS tv, ISNULL(a.n, 0) AS sn, ISNULL(b.n, 0) AS tn
              FROM a FULL OUTER HASH JOIN b ON ISNULL(a.v, N'{NULL_MARK}') = ISNULL(b.v, N'{NULL_MARK}'))"""
    # extra_empty: target rows with no id beyond the source's empty rows; in_list: source rows
    # without an equal target value whose value is in the list (so an id could have been given).
    r = dc._one(f"""{base} SELECT SUM(sn) AS source_rows, SUM(tn) AS target_rows,
        SUM(CASE WHEN sn < tn THEN sn ELSE tn END) AS same,
        SUM(CASE WHEN sn > tn THEN sn - tn ELSE 0 END) AS only_s,
        SUM(CASE WHEN tn > sn THEN tn - sn ELSE 0 END) AS only_t,
        SUM(CASE WHEN tv IS NULL AND sv IS NULL AND tn > sn THEN tn - sn ELSE 0 END) AS extra_empty,
        SUM(CASE WHEN sn > tn AND sv IS NOT NULL AND lst.v IS NOT NULL THEN sn - tn ELSE 0 END) AS in_list
        FROM d {_list_values(lk, 'lst', 'd.sv')}""")
    out = {"source_rows": _n(r["source_rows"]), "target_rows": _n(r["target_rows"]), "same": _n(r["same"]),
           "only_in_source": _n(r["only_s"]), "only_in_target": _n(r["only_t"]),
           "extra_empty": _n(r["extra_empty"]), "in_list": _n(r["in_list"]), "examples": {"source": [], "target": []}}
    if out["only_in_source"] or out["only_in_target"]:
        for x in dc._run(f"""{base}
            SELECT * FROM (SELECT TOP 5 'source' AS side, LEFT(sv, 120) AS v, sn - tn AS n
                           FROM d WHERE sn > tn ORDER BY sn - tn DESC) p
            UNION ALL
            SELECT * FROM (SELECT TOP 5 'target' AS side, LEFT(tv, 120) AS v, tn - sn AS n
                           FROM d WHERE tn > sn ORDER BY tn - sn DESC) q"""):
            out["examples"][x["side"]].append({"value": x["v"], "rows": _n(x["n"])})
    return out


def _agreement(prep, links):
    """Target rows only: does the copy column hold the value the id points at?"""
    todo = [lk for lk in links if lk.copy]
    if not todo:
        return {}
    tgt = _target_rel(prep, None, todo, None)
    exprs = [f"""CASE WHEN tc{lk.j} IS NULL AND tid{lk.j} IS NULL THEN {A['both_empty']}
                 WHEN tid{lk.j} IS NULL THEN CASE WHEN c{lk.j}.v IS NULL THEN {A['code_not_in_list']} ELSE {A['code_not_linked']} END
                 WHEN tf{lk.j} = 0 THEN {A['orphan']}
                 WHEN tc{lk.j} IS NULL THEN {A['code_empty']}
                 WHEN tv{lk.j} COLLATE {BIN} = tc{lk.j} COLLATE {BIN} THEN {A['agree']}
                 WHEN tv{lk.j} = tc{lk.j} THEN {A['case_only']}
                 ELSE {A['disagree']} END AS a{lk.j}""" for lk in todo]
    joins = " ".join(_list_values(lk, f"c{lk.j}", f"t.tc{lk.j}") for lk in todo)
    base = f"WITH t AS {tgt}, q AS (SELECT t.*, {', '.join(exprs)} FROM t {joins})"
    r = dc._one(f"{base} SELECT " + ", ".join(f"SUM(CASE WHEN a{lk.j} = {c} THEN 1 ELSE 0 END) AS a{lk.j}_{c}"
                                              for lk in todo for c in A.values()) + " FROM q")
    out = {}
    for lk in todo:
        counts = {name: _n(r[f"a{lk.j}_{c}"]) for name, c in A.items()}
        examples = []
        if counts["disagree"] or counts["code_not_linked"]:
            examples = [{"copy": x["c"], "target_id": x["i"], "target_value": x["v"], "rows": _n(x["n"])}
                        for x in dc._run(
                            f"{base} SELECT TOP 5 LEFT(tc{lk.j}, 120) AS c, LEFT(tid{lk.j}, 60) AS i, "
                            f"LEFT(tv{lk.j}, 120) AS v, COUNT_BIG(*) AS n FROM q "
                            f"WHERE a{lk.j} IN ({A['disagree']}, {A['code_not_linked']}) "
                            f"GROUP BY tc{lk.j}, tid{lk.j}, tv{lk.j} ORDER BY n DESC")]
        out[lk.j] = {"column": lk.copy["name"], "counts": counts, "examples": examples}
    return out


def _duplicates(lk):
    """Values that appear on more than one row of the list (a source value could then be
    linked to either row)."""
    if lk.mode == "code":
        return None
    e = _txt("r." + _q(lk.r["name"]), lk.r["type"])
    ref = _full(TARGET, lk.ref["schema"], lk.ref["table"])
    rows = dc._run(f"SELECT TOP 3 {e} AS v, COUNT_BIG(*) AS n FROM {ref} r WHERE {e} IS NOT NULL "
                   f"GROUP BY {e} HAVING COUNT_BIG(*) > 1 ORDER BY n DESC")
    if not rows:
        return None
    count = _n(dc._one(f"SELECT COUNT_BIG(*) AS n FROM (SELECT {e} AS v FROM {ref} r WHERE {e} IS NOT NULL "
                       f"GROUP BY {e} HAVING COUNT_BIG(*) > 1) d")["n"])
    return {"values": count, "examples": [{"value": r["v"], "rows": _n(r["n"])} for r in rows]}


# ---- findings -----------------------------------------------------------------------------------

def _v(value):
    return "empty" if value is None else f"'{value}'"


def _link_findings(lk, res, rows_equal):
    """(verdict, [(severity, text)], summary sentence) of one checked link, in plain words."""
    b, g, d = res["buckets"], res["groups"], res["distinct"]
    label, ref, via, F = lk.label, lk.ref_name, lk.via, lk.f["name"]
    meaning = lk.mode == "meaning"
    fs = []

    def eg(bucket, show, limit=3):
        return "; ".join(show(x) for x in g.get(bucket, [])[:limit])

    def many(bucket):
        n = d.get(bucket, 0)
        return f" ({_fmt(n)} different values)" if n > 1 else ""

    def nrows(n):
        return "1 row" if n == 1 else f"{_fmt(n)} rows"

    def s(n):        # a verb's -s: "1 row points", "2 rows point"
        return "s" if n == 1 else ""

    def have(n):
        return "has" if n == 1 else "have"

    if b["wrong"]:
        n = b["wrong"]
        show = ((lambda x: f"old id {x['old']} = {_v(x['source'])} → new id {x['target_id']} = {_v(x['target_value'])}")
                if meaning else (lambda x: f"{_v(x['source'])} → id {x['target_id']} = {_v(x['target_value'])}"))
        fs.append(("error", f"{label}: {nrows(n)} point{s(n)} to a different {ref} row than the source value "
                            f"— e.g. {eg('wrong', show)}."))
    if b["not_filled"]:
        n = b["not_filled"]
        fs.append(("error", f"{label}: {nrows(n)} {have(n)} no {F}, although the source value is in "
                            f"{via}{many('not_filled')} — e.g. {eg('not_filled', lambda x: _v(x['source']))}. "
                            "The link was not filled in."))
    if b["orphan"]:
        n = b["orphan"]
        fs.append(("error", f"{label}: {nrows(n)} hold{s(n)} a {F} value that does not exist in {ref} — e.g. id "
                            f"{eg('orphan', lambda x: str(x['target_id']))}."))
    if b["not_in_list"]:
        n = b["not_in_list"]
        kept = (f" The value itself is kept in {lk.copy['name']}." if lk.copy
                else " No paired column of the new table keeps the value.")
        fs.append(("review" if lk.copy else "error",
                   f"{label}: {nrows(n)} {have(n)} a source value that is not in {via}"
                   f"{many('not_in_list')} — e.g. {eg('not_in_list', lambda x: _v(x['source']))} — so {F} is empty.{kept}"))
    id_value = lambda x: f"id {x['target_id']} = {_v(x['target_value'])}"
    source_to_value = lambda x: f"{_v(x['source'])} → {_v(x['target_value'])}"
    if b["added"]:
        n = b["added"]
        fs.append(("review", f"{label}: {nrows(n)} {have(n)} {F} set although the source value is empty — e.g. "
                             f"{eg('added', id_value)}."))
    if b["old_broken"]:
        n = b["old_broken"]
        fs.append(("review", f"{label}: {nrows(n)} of the source hold{s(n)} an id that is not in the old list "
                             f"{lk.old['schema']}.{lk.old['table']} — e.g. {eg('old_broken', lambda x: _v(x['source']))}."))
    if b["case_only"]:
        n = b["case_only"]
        fs.append(("info", f"{label}: {nrows(n)} point{s(n)} to the right row, but the value is written "
                           f"differently — e.g. {eg('case_only', source_to_value, 2)}; {ref} keeps one spelling."))

    rem = res.get("remainder")
    if rem and (rem["source_rows"] or rem["target_rows"]):
        n = rem["source_rows"]
        if rem["only_in_source"] or rem["only_in_target"]:
            k = rem["only_in_source"]
            ex = ", ".join(f"{_v(x['value'])} ({_fmt(x['rows'])})" for x in rem["examples"]["source"][:3])
            tx = ", ".join(f"{_v(x['value'])} ({_fmt(x['rows'])})" for x in rem["examples"]["target"][:3])
            note = "" if rows_equal else " The row counts differ, so some of these may be missing or extra rows (see the data check)."
            how = f" ({nrows(n)} could not be paired one to one, so this comes from value counts.)"
            empty, in_list = rem.get("extra_empty", 0), rem.get("in_list", 0)
            if empty and empty >= k:
                # Mostly: the target has no id where the source has a value.
                share = f"all {_fmt(in_list)}" if in_list >= empty else _fmt(in_list)
                filled = (f"; {share} of those source values {'is' if in_list == 1 else 'are'} in {via}, "
                          "so the link was not filled in" if in_list else "")
                fs.append(("error" if rows_equal else "review",
                           f"{label}: {F} is empty on {nrows(empty)} where the source has a value (e.g. {ex}){filled}."
                           f"{how}{note}"))
            else:
                fs.append(("error" if rows_equal else "review",
                           f"{label}: {nrows(k)} of the source {have(k)} no equal value in the target"
                           f"{f' (source e.g. {ex})' if ex else ''}{f'; target e.g. {tx}' if tx else ''}.{how}{note}"))
        else:
            fs.append(("info", f"{label}: {nrows(n)} could not be paired one to one; compared by value counts, "
                               "they match."))

    agr = res.get("agreement")
    if agr:
        c, C = agr["counts"], agr["column"]
        if c["disagree"]:
            ex = "; ".join(f"{C} {_v(x['copy'])} but id {x['target_id']} = {_v(x['target_value'])}"
                           for x in agr["examples"][:2])
            fs.append(("error", f"{C} and {F} disagree on {nrows(c['disagree'])} of the new table — e.g. {ex}."))
        if c["code_not_linked"] and not b["not_filled"]:
            n = c["code_not_linked"]
            fs.append(("error", f"{nrows(n)} of the new table {have(n)} {C} set to a value that is in {ref}, "
                                f"but {F} is empty."))
        if c["code_not_in_list"] and not b["not_in_list"]:
            n = c["code_not_in_list"]
            fs.append(("review", f"{nrows(n)} of the new table {have(n)} {C} set to a value that is not in {ref}, "
                                 f"so {F} is empty."))
        if c["code_empty"]:
            n = c["code_empty"]
            fs.append(("review", f"{nrows(n)} of the new table {have(n)} {F} set, but {C} is empty."))
        both = c["agree"] + c["case_only"]
        if not c["disagree"] and both:
            fs.append(("info", f"{C} agrees with {F} on all {nrows(both)} of the new table where both are filled."))

    dup = res.get("duplicates")
    if dup:
        ex = ", ".join(f"{_v(x['value'])} ×{x['rows']}" for x in dup["examples"][:2])
        fs.append(("info", f"{_fmt(dup['values'])} values appear on more than one row of {ref} (e.g. {ex}), so such a "
                           "value could be linked to either row."))
    if lk.how == "found" and lk.coverage:
        fs.append(("info", f"{lk.s['name']} was found by its values: {_fmt(lk.coverage['found'])} of its "
                           f"{_fmt(lk.coverage['distinct'])} different values are in {via}"
                           f"{f' (first {DISCOVERY_SAMPLE:,} rows read)' if lk.coverage.get('sampled') else ''}."))

    with_value = res["compared"] - b["both_empty"] - b["added"] - b["old_broken"]
    good = b["correct"] + b["case_only"]
    what = (f"hold the same value as the source, and it exists in {ref}" if lk.mode == "code"
            else f"point to the right {ref} row (checked through {via})")
    empty = f"; {_fmt(b['both_empty'])} rows are empty on both sides" if b["both_empty"] else ""
    summary = (f"{label}: {_fmt(good)} of {_fmt(with_value)} rows with a source value {what}{empty}."
               if res["compared"] else f"{label}: no rows could be paired, so nothing was compared.")

    if any(s == "error" for s, _ in fs):
        return "problem", fs, summary
    if any(s == "review" for s, _ in fs):
        return "review", fs, summary
    return "ok", fs, summary


# ---- one mapping -------------------------------------------------------------------------------

def check(m, entry_of, plan):
    """Key mapping check of one mapping. `entry_of(member)` returns its live table entry."""
    started = time.time()
    result = {"mapping": m.id, "type": m.type, "checked_at": _now()}
    try:
        result.update(_check(m, entry_of, plan))
    except (dc.Skipped, db.TableLocked) as exc:
        text = (str(exc) if isinstance(exc, dc.Skipped)
                else "A table is locked by another session (a load in progress?). Try again later.")
        result.update(status="skipped", headline=text, links=[], findings=[{"severity": "info", "text": text}])
    result["seconds"] = round(time.time() - started, 1)
    return result


def _link_view(lk):
    return {
        "fk": lk.fk["name"], "column": lk.f["name"],
        "ref": {"schema": lk.ref["schema"], "table": lk.ref["table"], "key": lk.k["name"] if lk.k else None,
                "rows": lk.ref.get("rows")},
        "source": lk.s["name"] if lk.s else None, "source_type": lk.s["type"] if lk.s else None,
        "found_by": ("values" if lk.how == "found" else "pairing") if lk.s else None,
        "label": lk.r["name"] if lk.r else None, "mode": lk.mode, "via": lk.via if lk.mode else None,
        "copy": lk.copy["name"] if lk.copy else None, "old": lk.old, "coverage": lk.coverage,
    }


def _hide_values(res):
    """Contact-type columns: keep the counts, hide the values."""
    for items in res["groups"].values():
        for x in items:
            for k in ("source", "old", "target_value"):
                if x.get(k) is not None:
                    x[k] = HIDDEN
    for side in (res.get("remainder") or {}).get("examples", {}).values():
        for x in side:
            if x["value"] is not None:
                x["value"] = HIDDEN
    for x in (res.get("agreement") or {}).get("examples", []):
        x["copy"], x["target_value"] = HIDDEN, HIDDEN
    if res.get("duplicates"):
        for x in res["duplicates"]["examples"]:
            x["value"] = HIDDEN


def _check(m, entry_of, plan):
    prep = dc.prepare(m, entry_of)
    out = {"source": " + ".join(f"{e['schema']}.{e['table']}" for e in prep.sentries),
           "target": f"{prep.tentry['schema']}.{prep.tentry['table']}",
           "method": None, "key": None, "rows": None, "spec": None}
    links = _links(prep)
    if not links:
        return {**out, "status": "none", "links": [], "findings": [],
                "headline": f"{out['target']} has no links to other lists (no foreign keys)."}

    _find_sources(prep, links)
    for lk in links:
        if lk.mode == "code":
            old = _old_list(prep, lk, plan, entry_of)
            if old:
                lk.old, lk.mode = old, "meaning"
    _copy_columns(prep, links)
    ready = [lk for lk in links if lk.mode and not lk.reason]

    results, rows, rows_equal, top = {}, None, True, []
    if ready:
        key, notes = _key(prep)
        anchors = _anchors(prep, ready) if key is None else []
        labels = _label_pairs(anchors)
        head, full = _ctes(prep, key, ready, anchors, labels)
        sums = ["SUM(CASE WHEN sp = 1 AND tp = 1 THEN 1 ELSE 0 END) AS matched",
                "SUM(CASE WHEN sp = 1 AND tp IS NULL THEN 1 ELSE 0 END) AS missing",
                "SUM(CASE WHEN tp = 1 AND sp IS NULL THEN 1 ELSE 0 END) AS extra"]
        sums += [f"SUM(CASE WHEN b{lk.j} = {c} THEN 1 ELSE 0 END) AS n{lk.j}_{c}" for lk in ready for c in B.values()]
        r = dc._one(f"{full} SELECT {', '.join(sums)} FROM z")
        if key is not None:
            rows = {"source": _n(r["matched"]) + _n(r["missing"]), "target": _n(r["matched"]) + _n(r["extra"]),
                    "matched": _n(r["matched"]), "missing_in_target": _n(r["missing"]),
                    "extra_in_target": _n(r["extra"])}
            out.update(method="key", key={"source": key.source["name"], "target": key.target["name"]})
            if rows["missing_in_target"] or rows["extra_in_target"]:
                top.append(("info", f"Rows are paired on {key.source['name']} = {key.target['name']}: "
                                    f"{_fmt(rows['missing_in_target'])} source rows have no target row and "
                                    f"{_fmt(rows['extra_in_target'])} target rows have no source row, so their links "
                                    "are not compared here (the data check reports those rows)."))
        else:
            shape = dc._one(f"{head} SELECT (SELECT COUNT_BIG(*) FROM s0) AS sn, (SELECT COUNT_BIG(*) FROM t0) AS tn")
            sn, tn, paired = _n(shape["sn"]), _n(shape["tn"]), _n(r["matched"])
            rows = {"source": sn, "target": tn, "matched": paired, "anchor_columns": len(anchors),
                    "unpaired_source": sn - paired, "unpaired_target": tn - paired}
            out["method"] = "columns"
            rest = ", the rest compared by value counts" if paired < max(sn, tn) else ""
            top.append(("info", f"No column is unique on both sides, so rows were paired on the {len(anchors)} other "
                                f"columns that hold the same values in both tables: {_fmt(paired)} of {_fmt(sn)} source "
                                f"rows paired one to one{rest}."))
        rows_equal = rows["source"] == rows["target"]
        out["rows"] = rows
        out["key_notes"] = notes
        out["spec"] = {"key": [key.source["name"], key.target["name"]] if key is not None else None,
                       "anchors": [[a.source["name"], a.target["name"]] for a in anchors],
                       "labels": [[a.source["name"], a.target["name"]] for a in labels]}

        agreement = _agreement(prep, ready)
        for lk in ready:
            buckets = {name: _n(r[f"n{lk.j}_{c}"]) for name, c in B.items()}
            res = {"buckets": buckets, "compared": sum(buckets.values()), "groups": {}, "distinct": {}}
            if any(buckets[x] for x in ("case_only",) + PROBLEM_BUCKETS):
                res["groups"], res["distinct"] = _groups(prep, key, lk, anchors, labels)
            res["remainder"] = (_remainder(prep, lk, anchors, labels)
                                if key is None and (rows["unpaired_source"] or rows["unpaired_target"]) else None)
            res["agreement"] = agreement.get(lk.j)
            res["duplicates"] = _duplicates(lk)
            if is_sensitive(lk.s["name"], lk.f["name"], lk.r["name"]):
                _hide_values(res)
            results[lk.j] = res

    views, findings = [], []
    for lk in links:
        view = _link_view(lk)
        if lk.j in results:
            verdict, fs, summary = _link_findings(lk, results[lk.j], rows_equal)
            view.update(results[lk.j], spec=lk.spec(), summary=summary)
        else:
            verdict, fs = "not_checked", [("info", f"{lk.f['name']} → {lk.ref_name}: {lk.reason}")]
            view["reason"] = lk.reason
        view["verdict"] = verdict
        view["findings"] = [{"severity": s, "text": t} for s, t in fs]
        findings += [{"severity": s, "text": t, "column": lk.f["name"]} for s, t in fs]
        views.append(view)
    findings = [{"severity": s, "text": t} for s, t in top] + findings

    checked = [v for v in views if v["verdict"] != "not_checked"]

    def first(sev):
        return next((f["text"] for f in findings if f["severity"] == sev), None)

    if first("error"):
        status, headline = "problems", first("error")
    elif first("review"):
        status, headline = "review", first("review")
    elif checked:
        status = "ok"
        rest = len(views) - len(checked)
        headline = ("The link points to the right rows." if len(views) == 1 else
                    f"All {len(views)} links point to the right rows." if not rest else
                    f"{len(checked)} of {len(views)} links checked, and they point to the right rows; "
                    f"{rest} could not be checked.")
    else:
        status = "not_checked"
        headline = "No link could be checked: " + (views[0].get("reason") or "")
    return {**out, "status": status, "headline": headline, "links": views, "findings": findings}


# ---- the rows behind one link ---------------------------------------------------------------------

ROW_FILTERS = ("problems", "all") + BUCKETS


def rows(m, entry_of, saved, column, flt="problems", page=0, size=50, reveal=False):
    """The paired rows behind one link of the last mapping check: one bucket (or every
    problem) at a time, paged inside SQL Server. The saved check says what to compare, so
    the rows always agree with its counts."""
    if not saved or not saved.get("links"):
        raise NotReady("Run the mapping check of this table first.")
    lr = next((x for x in saved["links"] if x["column"].lower() == column.lower()), None)
    if lr is None:
        raise LookupError(f"No link on column {column!r} in the last mapping check.")
    if not lr.get("spec"):
        raise NotReady(lr.get("reason") or "This link was not checked.")
    if flt not in ROW_FILTERS:
        raise LookupError(f"Unknown filter {flt!r}; use one of {', '.join(ROW_FILTERS)}.")
    prep = dc.prepare(m, entry_of)
    by_names = {(p.source["name"].lower(), p.target["name"].lower()): p for p in prep.pairs}

    def pick(names):
        p = by_names.get((names[0].lower(), names[1].lower()))
        if p is None:
            raise NotReady("The table's columns changed since the last mapping check. Run it again.")
        return p

    spec = saved.get("spec") or {}
    key = pick(spec["key"]) if spec.get("key") else None
    anchors = [pick(a) for a in spec.get("anchors") or []] if key is None else []
    labels = [pick(a) for a in spec.get("labels") or []] if key is None else []
    lk = Link.from_spec(lr["spec"])
    _, full = _ctes(prep, key, [lk], anchors, labels)
    problems = ", ".join(str(B[b]) for b in PROBLEM_BUCKETS)
    where = {"problems": f"b0 IN ({problems})", "all": "b0 IS NOT NULL"}.get(flt) or f"b0 = {B[flt]}"
    total = _n(dc._one(f"{full} SELECT COUNT_BIG(*) AS n FROM z WHERE {where}")["n"])
    data = dc._run(f"""{full}
        SELECT b0 AS b, CONVERT(nvarchar(200), sk) AS k, LEFT(sv0, 500) AS s, LEFT(sr0, 500) AS o,
               LEFT(tid0, 100) AS i, LEFT(tv0, 500) AS t
        FROM z WHERE {where}
        ORDER BY CASE WHEN b0 IN ({problems}) THEN 0 ELSE 1 END, b0, sk, sv0
        OFFSET ? ROWS FETCH NEXT ? ROWS ONLY""", (page * size, size))
    sensitive = is_sensitive(lk.s["name"], lk.f["name"], lk.r["name"])
    hide = sensitive and not reveal

    def clip(v):
        return HIDDEN if hide and v is not None else v

    return {
        "mapping": m.id, "column": lk.f["name"], "source": lk.s["name"], "via": lk.via, "mode": lk.mode,
        "method": saved.get("method"), "key": saved.get("key"), "filter": flt, "page": page, "size": size,
        "total": total, "sensitive": sensitive, "hidden": hide, "checked_at": saved.get("checked_at"),
        "rows": [{"row": x["k"], "source": clip(x["s"]), "old": clip(x["o"]) if lk.mode == "meaning" else None,
                  "target_id": x["i"], "target_value": clip(x["t"]), "status": BUCKETS[x["b"]]} for x in data],
    }
