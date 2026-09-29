"""The real values behind a data check, for one column of one mapping.

Two views, both read-only and paged inside SQL Server:

  rows    (mappings matched on a key) each source row next to its own target row: the
          key, the source value, the target value and the same verdict the data check
          gives that cell (identical, lost, different ...), plus rows missing or extra.
  counts  (every mapping) each distinct value with how many rows hold it on each side -
          the only honest view when rows cannot be paired.

It reuses the data check's own pairing, type conversion, key and lookup translation
(`datacheck.prepare` + the saved result), so what is shown always agrees with the verdict.
Columns that look like contact details are hidden unless the caller asks to reveal them.
"""
import csv
import io
import re

from . import datacheck as dc

# Contact-type columns: e-mail addresses and phone numbers.
SENSITIVE = re.compile(r"e_?mail|phone|mobile|contact|whatsapp|gsm|fax|(^|_)tel(_|$)", re.IGNORECASE)
HIDDEN = "•••• hidden"
MAX_PAGE_SIZE = 200
CSV_MAX_ROWS = 100_000
TEXT_LIMIT = 2000   # characters of one value sent to the browser

ROW_FILTERS = {
    "all": "1 = 1",
    "differences": "(g <> 0 OR sp IS NULL OR tp IS NULL)",
    "lost": "g = 4",
    "different": "g = 6",
    "recoded": "g = 5",
    "missing": "sp = 1 AND tp IS NULL",
    "extra": "tp = 1 AND sp IS NULL",
}
COUNT_FILTERS = {
    "all": "1 = 1",
    "differences": "sn <> tn",
}


class NotReady(Exception):
    """The values cannot be shown yet; the message says why."""


def is_sensitive(*names):
    return any(n and SENSITIVE.search(n) for n in names)


def _setup(m, entry_of, saved, column):
    """(prepared mapping, the column's pair, the key pair or None) - key and lookups taken
    from the saved data check, so the view compares exactly what the check compared."""
    if not saved:
        raise NotReady("Run the data check of this table first.")
    if saved.get("status") in ("skipped", "error"):
        raise NotReady(saved.get("headline") or "This mapping has no data check.")
    prep = dc.prepare(m, entry_of)
    lowered = column.lower()
    pair = (next((p for p in prep.pairs if p.source["name"].lower() == lowered), None)
            or next((p for p in prep.pairs if p.target["name"].lower() == lowered), None))
    if pair is None:
        raise LookupError(f"{column!r} is not a compared column of this mapping.")
    saved_cols = {c["source"].lower(): c for c in saved.get("columns", [])}
    for p in prep.pairs:
        lk = saved_cols.get(p.source["name"].lower(), {}).get("lookup")
        if lk:
            p.lookup = lk
    key = None
    k = saved.get("key")
    if k:
        key = next((p for p in prep.pairs
                    if p.source["name"] == k["source"] and p.target["name"] == k["target"]), None)
    return prep, pair, key


def _side(prep, pairs):
    """The mapping's relations with only the columns a query needs."""
    return dc.Side(prep.side.sources, prep.side.target, pairs)


def _clip(v):
    if v is None:
        return None, False
    return (v[:TEXT_LIMIT], True) if len(v) > TEXT_LIMIT else (v, False)


def _rows_base(prep, pair, key):
    wanted = [pair] if pair is key else [pair, key]
    side = _side(prep, wanted)
    src, tgt = side.source_sql(key_pair=key), side.target_sql(key_pair=key, raw=True)
    i = pair.i
    return f"""WITH s AS {src}, t AS {tgt},
        m AS (SELECT s.__src AS src_table, s.__s AS sp, t.__t AS tp, s.__k AS sk, t.__k AS tk,
                     s.r{i} AS sv, t.u{i} AS tv, t.v{i} AS tr, {dc._bucket_expr(pair)} AS g
              FROM s FULL OUTER JOIN t ON s.__k = t.__k)"""


def _row_status(r):
    if r["sp"] is None:
        return "extra"
    if r["tp"] is None:
        return "missing"
    return dc.BUCKETS[r["g"]]


def rows(m, entry_of, saved, column, flt="all", q="", page=0, size=50, reveal=False, limit=None):
    """A page of source rows next to their target rows, for one column."""
    prep, pair, key = _setup(m, entry_of, saved, column)
    if key is None:
        raise NotReady("This table has no column that identifies a row on both sides, so rows cannot be "
                       "paired. Use Value counts instead.")
    where = ROW_FILTERS.get(flt, ROW_FILTERS["all"])
    params = []
    if q:
        where += (" AND (CONVERT(nvarchar(4000), COALESCE(sk, tk)) LIKE ? OR sv LIKE ? OR tv LIKE ? "
                  "OR tr LIKE ?)")
        params += [f"%{q}%"] * 4
    base = _rows_base(prep, pair, key)
    order = "ORDER BY CASE WHEN sk IS NULL THEN 1 ELSE 0 END, sk, tk"
    if limit is None:
        size = max(1, min(int(size), MAX_PAGE_SIZE))
        page = max(0, int(page))
        paging = "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        params += [page * size, size]
        top = ""
    else:
        paging, top = "", f"TOP ({int(limit)})"
    data = dc._run(f"""{base}
        SELECT {top} src_table, CONVERT(nvarchar(200), COALESCE(sk, tk)) AS k, sp, tp, g, sv, tv, tr,
               COUNT(*) OVER () AS total
        FROM m WHERE {where} {order} {paging}""", tuple(params))
    counts = dc._one(f"""{base} SELECT COUNT_BIG(*) AS all_rows,
        SUM(CASE WHEN {ROW_FILTERS['differences']} THEN 1 ELSE 0 END) AS differences,
        SUM(CASE WHEN g = 4 THEN 1 ELSE 0 END) AS lost,
        SUM(CASE WHEN g = 6 THEN 1 ELSE 0 END) AS different,
        SUM(CASE WHEN g = 5 THEN 1 ELSE 0 END) AS recoded,
        SUM(CASE WHEN sp = 1 AND tp IS NULL THEN 1 ELSE 0 END) AS missing,
        SUM(CASE WHEN tp = 1 AND sp IS NULL THEN 1 ELSE 0 END) AS extra
        FROM m""") if limit is None else {}
    hidden = is_sensitive(pair.source["name"], pair.target["name"]) and not reveal
    out = []
    for r in data:
        sv, s_cut = _clip(r["sv"])
        tv, t_cut = _clip(r["tv"])
        out.append({
            "key": r["k"],
            "table": r["src_table"],
            "source": HIDDEN if hidden and sv is not None else sv,
            "target": HIDDEN if hidden and tv is not None else tv,
            # the stored id behind a translated lookup value, e.g. 39 for 'Al Tasnim ...'
            "target_raw": (HIDDEN if hidden else r["tr"]) if pair.lookup and r["tr"] is not None else None,
            "truncated": s_cut or t_cut,
            "status": _row_status(r),
        })
    return {
        "view": "rows",
        "column": {"source": pair.source["name"], "target": pair.target["name"], "stype": pair.stype,
                   "ttype": pair.ttype, "lookup": pair.lookup},
        "key": {"source": key.source["name"], "target": key.target["name"]},
        "union": len(prep.srcs) > 1,
        "hidden": hidden,
        "sensitive": is_sensitive(pair.source["name"], pair.target["name"]),
        "filter": flt,
        "total": dc._n(data[0]["total"]) if data else 0,
        "page": page,
        "size": size,
        "counts": {k: dc._n(v) for k, v in counts.items()},
        "rows": out,
    }


def counts(m, entry_of, saved, column, flt="all", q="", page=0, size=50, reveal=False, limit=None):
    """Each distinct value of one column with how many rows hold it in source and target."""
    prep, pair, _key = _setup(m, entry_of, saved, column)
    side = _side(prep, [pair])
    src, tgt = side.source_sql(), side.target_sql(raw=True)
    i = pair.i
    fs, ft = dc._fingerprint_part(f"x.c{i}", pair), dc._fingerprint_part(f"y.t{i}", pair)
    base = f"""WITH s AS (SELECT {fs} AS v, COUNT_BIG(*) AS n, MIN(x.r{i}) AS d FROM {src} x GROUP BY {fs}),
                    t AS (SELECT {ft} AS v, COUNT_BIG(*) AS n, MIN(y.u{i}) AS d, MIN(y.v{i}) AS r FROM {tgt} y GROUP BY {ft}),
                    c AS (SELECT COALESCE(s.d, t.d) AS value, t.r AS raw,
                                 CASE WHEN COALESCE(s.v, t.v) = N'{dc.NULL_MARK}' THEN 1 ELSE 0 END AS is_null,
                                 ISNULL(s.n, 0) AS sn, ISNULL(t.n, 0) AS tn
                          FROM s FULL OUTER JOIN t ON s.v = t.v)"""
    where = COUNT_FILTERS.get(flt, COUNT_FILTERS["all"])
    params = []
    if q:
        where += " AND (value LIKE ? OR raw LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    order = "ORDER BY CASE WHEN sn <> tn THEN 0 ELSE 1 END, sn + tn DESC, value"
    if limit is None:
        size = max(1, min(int(size), MAX_PAGE_SIZE))
        page = max(0, int(page))
        paging, top = "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", ""
        params += [page * size, size]
    else:
        paging, top = "", f"TOP ({int(limit)})"
    data = dc._run(f"""{base} SELECT {top} value, raw, is_null, sn, tn, COUNT(*) OVER () AS total
                       FROM c WHERE {where} {order} {paging}""", tuple(params))
    totals = dc._one(f"""{base} SELECT COUNT_BIG(*) AS distinct_values,
                         SUM(CASE WHEN sn <> tn THEN 1 ELSE 0 END) AS differing_values,
                         SUM(sn) AS source_rows, SUM(tn) AS target_rows FROM c""") if limit is None else {}
    hidden = is_sensitive(pair.source["name"], pair.target["name"]) and not reveal
    out = []
    for r in data:
        v, cut = _clip(r["value"])
        out.append({
            "value": None if r["is_null"] else (HIDDEN if hidden else v),
            "target_raw": (HIDDEN if hidden else r["raw"]) if pair.lookup and not r["is_null"] else None,
            "is_null": bool(r["is_null"]),
            "truncated": cut,
            "source_rows": dc._n(r["sn"]),
            "target_rows": dc._n(r["tn"]),
        })
    return {
        "view": "counts",
        "column": {"source": pair.source["name"], "target": pair.target["name"], "stype": pair.stype,
                   "ttype": pair.ttype, "lookup": pair.lookup},
        "hidden": hidden,
        "sensitive": is_sensitive(pair.source["name"], pair.target["name"]),
        "filter": flt,
        "total": dc._n(data[0]["total"]) if data else 0,
        "page": page,
        "size": size,
        "totals": {k: dc._n(v) for k, v in totals.items()},
        "values": out,
    }


def as_csv(result):
    """The rows of a `rows` or `counts` result as CSV text."""
    buf = io.StringIO()
    w = csv.writer(buf)
    col = result["column"]
    if result["view"] == "rows":
        k = result["key"]
        w.writerow([f"key ({k['source']} / {k['target']})"] + (["source table"] if result["union"] else [])
                   + [f"source {col['source']}", f"target {col['target']}"]
                   + (["target id"] if col["lookup"] else []) + ["result"])
        for r in result["rows"]:
            w.writerow([r["key"]] + ([r["table"]] if result["union"] else []) + [r["source"], r["target"]]
                       + ([r["target_raw"]] if col["lookup"] else []) + [r["status"]])
    else:
        w.writerow([f"value ({col['source']} / {col['target']})"] + (["target id"] if col["lookup"] else [])
                   + ["rows in source", "rows in target", "difference"])
        for r in result["values"]:
            w.writerow(["NULL" if r["is_null"] else r["value"]] + ([r["target_raw"]] if col["lookup"] else [])
                       + [r["source_rows"], r["target_rows"], r["target_rows"] - r["source_rows"]])
    return buf.getvalue()
