"""Tables and columns of an ATNM database against its RDS copy.

A copy keeps the names, so tables are matched on schema.table and columns on their name,
both ignoring upper/lower case as SQL Server does by default. Pure Python: the catalogs
come from app.ATNM.catalog.

Severities: problem (data may be missing or changed), review (a person should confirm
it), ok.
"""

SEVERITY = ("ok", "review", "problem")


def _worst(*levels):
    return max(levels, key=SEVERITY.index) if levels else "ok"


def compare_columns(src_cols, tgt_cols):
    """One row per column name on either side, in ATNM order, then the RDS-only columns.

    status: same | missing (only in ATNM) | extra (only in RDS) | changed (type,
    nullability, identity, computed or collation differs). `notes` says what differs.
    """
    by_name = {c["name"].lower(): c for c in tgt_cols}
    seen, rows = set(), []
    for s in src_cols:
        t = by_name.get(s["name"].lower())
        if t is None:
            rows.append({"name": s["name"], "source": s, "target": None, "status": "missing", "severity": "problem",
                         "notes": ["not in the RDS table"]})
            continue
        seen.add(t["name"].lower())
        notes, severity = [], "ok"
        if s["type"].lower() != t["type"].lower():
            notes.append(f"type {s['type']} → {t['type']}")
            severity = "problem"
        if s["nullable"] != t["nullable"]:
            notes.append("may now be empty" if t["nullable"] else "may no longer be empty")
            severity = _worst(severity, "review")
        if s["identity"] != t["identity"]:
            notes.append("identity added" if t["identity"] else "identity removed")
            severity = _worst(severity, "review")
        if s["computed"] != t["computed"]:
            notes.append("now computed" if t["computed"] else "no longer computed")
            severity = _worst(severity, "review")
        if (s["collation"] or "") != (t["collation"] or "") and s["collation"] and t["collation"]:
            notes.append(f"collation {s['collation']} → {t['collation']}")
            severity = _worst(severity, "review")
        if s["name"] != t["name"]:
            notes.append(f"name written {t['name']}")
        rows.append({"name": s["name"], "source": s, "target": t,
                     "status": "same" if severity == "ok" else "changed", "severity": severity, "notes": notes})
    for t in tgt_cols:
        if t["name"].lower() not in seen:
            rows.append({"name": t["name"], "source": None, "target": t, "status": "extra", "severity": "review",
                         "notes": ["only in the RDS table"]})
    return rows


def column_summary(rows):
    return {
        "source": sum(1 for r in rows if r["source"]),
        "target": sum(1 for r in rows if r["target"]),
        "same": sum(1 for r in rows if r["status"] == "same"),
        "missing": [r["name"] for r in rows if r["status"] == "missing"],
        "extra": [r["name"] for r in rows if r["status"] == "extra"],
        "changed": [{"name": r["name"], "notes": r["notes"], "severity": r["severity"]}
                    for r in rows if r["status"] == "changed"],
        "severity": _worst(*(r["severity"] for r in rows)),
    }


def signature(rows):
    """What a data check compared: the shared columns and their types on both sides. A saved
    check whose signature differs from today's was made on other columns and is out of date."""
    return "|".join(f"{r['name'].lower()}:{r['source']['type'].lower()}:{r['target']['type'].lower()}"
                    for r in rows if r["source"] and r["target"])


def _fmt(n):
    return f"{n:,}"


def _names(items, limit=4):
    items = list(items)
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def table_view(key, src, tgt, saved, cutoff=None, live=None):
    """One table of a pair as the UI lists it: what exists where, how the columns and row
    counts compare, the last data check, one overall status and the reason for it.

    status: problem | review | verified (values identical) | unverified (structure and
    counts agree, values not checked yet).

    A saved check is out of date (`stale`) when it was made on other columns, with another
    cutoff, or when the row count of either table has changed since: "verified" never
    outlives the data it was measured on. `live` ({source, target}: how the row counts
    moved lately, see catalog.movement) marks a table still being written to.
    """
    s_t = src or tgt
    view = {"key": key, "schema": s_t["schema"], "table": s_t["table"],
            "in_source": src is not None, "in_target": tgt is not None,
            "rows": {"source": src["rows"] if src else None, "target": tgt["rows"] if tgt else None, "exact": False},
            "columns": None, "row_key": (src or {}).get("row_key") or (tgt or {}).get("row_key"),
            "checks": {}, "data": None, "live": live if live and any(live.values()) else None}

    if src is None or tgt is None:
        where = "RDS" if src else "ATNM"
        view["checks"] = {"table": "problem" if src else "review", "columns": "none", "rows": "none", "data": "none"}
        view["status"] = "problem" if src else "review"
        view["reason"] = (f"Not in {where}: this table was not copied." if src
                          else "Only in RDS: the ATNM database has no table of this name.")
        return view

    rows = compare_columns(src["columns"], tgt["columns"])
    cols = column_summary(rows)
    view["columns"] = cols

    # The data check, while it still describes the tables as they are.
    data = None
    if saved:
        expected = signature(rows) + (f"|cutoff:{cutoff}" if cutoff else "")
        stale_reason = None
        if saved.get("signature") != expected:
            stale_reason = ("The cutoff changed since the last check." if "|cutoff:" in (saved.get("signature") or "")
                            or cutoff else "The columns changed since the last check.")
        measured = saved.get("rows") if saved.get("status") in ("identical", "different") else None
        changed = None
        if not stale_reason and measured and not cutoff:
            # Metadata row counts equal the exact counts of an unchanged table; a difference
            # means rows were added or removed after the check.
            if measured["source"] != src["rows"] or measured["target"] != tgt["rows"]:
                changed = {"source": {"then": measured["source"], "now": src["rows"]},
                           "target": {"then": measured["target"], "now": tgt["rows"]}}
                parts = [f"{side} {_fmt(c['then'])} → {_fmt(c['now'])}" for side, c in
                         (("ATNM", changed["source"]), ("RDS", changed["target"])) if c["then"] != c["now"]]
                stale_reason = f"Rows changed since the check ({', '.join(parts)})."
        data = {k: saved.get(k) for k in ("status", "headline", "checked_at", "seconds", "cutoff")}
        data["stale"] = bool(stale_reason)
        data["stale_reason"] = stale_reason
        data["changed_since"] = changed
        data["last_attempt"] = saved.get("last_attempt")
        if not stale_reason and measured:
            view["rows"] = {"source": measured["source"], "target": measured["target"], "exact": True,
                            "metadata_source": src["rows"], "metadata_target": tgt["rows"]}
    view["data"] = data

    n_src, n_tgt = view["rows"]["source"], view["rows"]["target"]
    rows_level = "ok" if n_src == n_tgt else "problem"
    if data is None or data["stale"]:
        data_level = "none"
    elif data["status"] == "identical":
        data_level = "ok"
    elif data["status"] == "different":
        data_level = "problem"
    else:   # error, timeout, changed, locked, skipped: not measured
        data_level = "none"
    view["checks"] = {"table": "ok", "columns": cols["severity"], "rows": rows_level, "data": data_level}

    reasons = []
    if cols["missing"]:
        reasons.append(("problem", f"{len(cols['missing'])} column{'s' if len(cols['missing']) != 1 else ''} "
                                   f"missing in RDS: {_names(cols['missing'])}"))
    type_changes = [c for c in cols["changed"] if c["severity"] == "problem"]
    if type_changes:
        reasons.append(("problem", f"{len(type_changes)} column type{'s' if len(type_changes) != 1 else ''} changed: "
                                   + _names(f"{c['name']} ({c['notes'][0]})" for c in type_changes)))
    if n_src != n_tgt:
        diff = n_tgt - n_src
        what = f"{_fmt(abs(diff))} {'more' if diff > 0 else 'fewer'} rows"
        reasons.append(("problem", f"RDS has {what} ({_fmt(n_src)} in ATNM, {_fmt(n_tgt)} in RDS"
                                   f"{'' if view['rows']['exact'] else ', table metadata'})"))
    if data_level == "problem":
        reasons.append(("problem", data["headline"]))
    if cols["extra"]:
        reasons.append(("review", f"{len(cols['extra'])} column{'s' if len(cols['extra']) != 1 else ''} only in RDS: "
                                  f"{_names(cols['extra'])}"))
    soft = [c for c in cols["changed"] if c["severity"] == "review"]
    if soft:
        reasons.append(("review", _names([f"{c['name']}: {', '.join(c['notes'])}" for c in soft], 3)))

    level = _worst(*(lvl for lvl, _ in reasons)) if reasons else "ok"
    if level != "ok":
        view["status"] = level
        view["reason"] = next(text for lvl, text in reasons if lvl == level)
        view["reasons"] = [{"severity": lvl, "text": text} for lvl, text in reasons]
        return view
    view["reasons"] = []
    if data_level == "ok":
        view["status"] = "verified"
        view["reason"] = data["headline"]
    else:
        view["status"] = "unverified"
        if data and data["stale"]:
            view["reason"] = f"{data['stale_reason']} Check the values again."
        elif data and data["status"] not in ("identical", "different"):
            view["reason"] = f"Values not checked: {data['headline']}"
        else:
            view["reason"] = (f"Same columns and the same row count ({_fmt(n_src)}); "
                              "the values have not been checked yet.")
    return view


def absent_view(key, schema, table):
    """A required table that neither database has."""
    return {"key": key, "schema": schema, "table": table, "in_source": False, "in_target": False,
            "rows": {"source": None, "target": None, "exact": False}, "columns": None, "row_key": None,
            "checks": {"table": "problem", "columns": "none", "rows": "none", "data": "none"}, "data": None,
            "status": "problem", "reasons": [],
            "reason": "Required by the migration plan, but neither database has a table of this name."}


def pair_tables(src_cat, tgt_cat, saved_of, cutoff_of=lambda key: None, live_of=lambda key: None):
    """Every table of both databases, ATNM tables first in name order, then RDS-only ones."""
    s, t = src_cat["tables"], tgt_cat["tables"]
    keys = sorted(s) + sorted(k for k in t if k not in s)
    return [table_view(k, s.get(k), t.get(k), saved_of(k), cutoff_of(k), live_of(k)) for k in keys]


def summarize(tables):
    by = {}
    for v in tables:
        by[v["status"]] = by.get(v["status"], 0) + 1
    return {
        "tables_source": sum(1 for v in tables if v["in_source"]),
        "tables_target": sum(1 for v in tables if v["in_target"]),
        "missing_in_target": sum(1 for v in tables if v["in_source"] and not v["in_target"]),
        "only_in_target": sum(1 for v in tables if v["in_target"] and not v["in_source"]),
        "missing_everywhere": sum(1 for v in tables if not v["in_source"] and not v["in_target"]),
        # results that no longer describe the table, and tables still being written to
        "out_of_date": sum(1 for v in tables if (v.get("data") or {}).get("stale")),
        "live": sum(1 for v in tables if v.get("live")),
        "verified": by.get("verified", 0),
        "unverified": by.get("unverified", 0),
        "review": by.get("review", 0),
        "problem": by.get("problem", 0),
        "rows_source": sum(v["rows"]["source"] or 0 for v in tables if v["in_source"]),
        "rows_target": sum(v["rows"]["target"] or 0 for v in tables if v["in_target"]),
    }
