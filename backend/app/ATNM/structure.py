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


def apply_renames(rows, saved):
    """compare_columns rows with the renames the last data check verified by their data
    (identical on every paired row) shown as one `renamed` column instead of a missing one
    plus an extra one. Only renames whose two columns still exist that way are applied."""
    found = [r for r in (saved or {}).get("renames") or [] if r.get("verdict") == "verified"]
    if not found:
        return rows
    missing = {r["name"].lower(): r for r in rows if r["status"] == "missing"}
    extra = {r["name"].lower(): r for r in rows if r["status"] == "extra"}
    use = {f["source"].lower(): f for f in found
           if f["source"].lower() in missing and f["target"].lower() in extra}
    used_targets = {f["target"].lower() for f in use.values()}
    out = []
    for r in rows:
        name = r["name"].lower()
        if r["status"] == "missing" and name in use:
            f = use[name]
            t = extra[f["target"].lower()]["target"]
            notes = [f"renamed to {t['name']}: identical on all {_fmt(f['paired'])} paired rows"]
            severity = "ok"
            if r["source"]["type"].lower() != t["type"].lower():
                notes.append(f"type {r['source']['type']} → {t['type']}")
                severity = "review"
            if r["source"]["nullable"] != t["nullable"]:
                notes.append("may now be empty" if t["nullable"] else "may no longer be empty")
                severity = "review"
            out.append({"name": r["name"], "source": r["source"], "target": t, "status": "renamed",
                        "severity": severity, "notes": notes})
        elif r["status"] == "extra" and name in used_targets:
            continue
        else:
            out.append(r)
    return out


def _cols(names):
    return tuple(n.lower() for n in names)


def _sql_text(definition):
    """A default or check definition as SQL Server stores it, without spacing, outer
    brackets or case, so two copies of the same rule compare equal."""
    text = "".join((definition or "").split()).lower()
    while text.startswith("(") and text.endswith(")"):
        inner, depth = text[1:-1], 0
        for ch in inner:
            depth += 1 if ch == "(" else -1 if ch == ")" else 0
            if depth < 0:
                return text
        text = inner
    return text


def compare_constraints(src, tgt):
    """The keys and rules of a table in ATNM against its RDS copy, matched on what they cover
    (columns, referenced table, definition), never on their names: a copy may name them
    differently. One row each: {kind, what, status, severity, note}.

    status: same | missing (not in RDS) | changed | extra (only in RDS). A missing or changed
    primary key is a problem (nothing stops duplicate rows any more); a missing unique key,
    foreign key, default or check rule is for review; one only in RDS is just listed.
    None when either side's keys could not be read.
    """
    if src is None or tgt is None:
        return None
    rows = []

    def add(kind, what, status, severity, note=""):
        rows.append({"kind": kind, "what": what, "status": status, "severity": severity, "note": note})

    sp, tp = src.get("primary_key"), tgt.get("primary_key")
    if sp and not tp:
        add("primary key", ", ".join(sp), "missing", "problem", "RDS has no primary key: duplicate rows are not prevented")
    elif sp and _cols(sp) != _cols(tp):
        add("primary key", ", ".join(sp), "changed", "problem", f"RDS has its primary key on {', '.join(tp)}")
    elif sp:
        add("primary key", ", ".join(sp), "same", "ok")
    elif tp:
        add("primary key", ", ".join(tp), "extra", "ok", "only in RDS")

    su = {_cols(u): u for u in src.get("unique") or []}
    tu = {_cols(u): u for u in tgt.get("unique") or []}
    for k, u in su.items():
        if k in tu:
            add("unique key", ", ".join(u), "same", "ok")
        else:
            add("unique key", ", ".join(u), "missing", "review", "not in RDS: duplicate values are not prevented")
    for k, u in tu.items():
        if k not in su:
            add("unique key", ", ".join(u), "extra", "ok", "only in RDS")

    def fk_key(f):
        return _cols(f["columns"]), f["ref_table"].lower(), _cols(f["ref_columns"])

    def fk_what(f):
        return f"{', '.join(f['columns'])} → {f['ref_table']}({', '.join(f['ref_columns'])})"

    sf = {fk_key(f): f for f in src.get("foreign_keys") or []}
    tf = {fk_key(f): f for f in tgt.get("foreign_keys") or []}
    for k, f in sf.items():
        t = tf.get(k)
        if t is None:
            add("foreign key", fk_what(f), "missing", "review", "not in RDS: the link is not enforced")
        elif f["enabled"] and not t["enabled"]:
            add("foreign key", fk_what(f), "changed", "review", "disabled in RDS: the link is not enforced")
        elif f["trusted"] and not t["trusted"]:
            add("foreign key", fk_what(f), "changed", "review", "not trusted in RDS: existing rows were never checked")
        else:
            add("foreign key", fk_what(f), "same", "ok")
    for k, f in tf.items():
        if k not in sf:
            add("foreign key", fk_what(f), "extra", "ok", "only in RDS")

    sd = {c.lower(): (c, d) for c, d in (src.get("defaults") or {}).items()}
    td = {c.lower(): (c, d) for c, d in (tgt.get("defaults") or {}).items()}
    for k, (c, d) in sd.items():
        if k not in td:
            add("default value", f"{c} = {d}", "missing", "review", "not in RDS: new rows get no default")
        elif _sql_text(d) != _sql_text(td[k][1]):
            add("default value", f"{c} = {d}", "changed", "review", f"RDS default is {td[k][1]}")
        else:
            add("default value", f"{c} = {d}", "same", "ok")
    for k, (c, d) in td.items():
        if k not in sd:
            add("default value", f"{c} = {d}", "extra", "ok", "only in RDS")

    sc = {_sql_text(c["definition"]): c for c in src.get("checks") or []}
    tc = {_sql_text(c["definition"]): c for c in tgt.get("checks") or []}
    for k, c in sc.items():
        if k not in tc:
            add("check rule", c["definition"], "missing", "review", "not in RDS: the rule is not enforced")
        elif c["enabled"] and not tc[k]["enabled"]:
            add("check rule", c["definition"], "changed", "review", "disabled in RDS")
        else:
            add("check rule", c["definition"], "same", "ok")
    for k, c in tc.items():
        if k not in sc:
            add("check rule", c["definition"], "extra", "ok", "only in RDS")
    return rows


def constraint_summary(rows):
    """Counts of compare_constraints rows, and their worst severity (None: not compared)."""
    if rows is None:
        return None
    return {
        "total": sum(1 for r in rows if r["status"] != "extra"),
        "same": sum(1 for r in rows if r["status"] == "same"),
        "missing": sum(1 for r in rows if r["status"] == "missing"),
        "changed": sum(1 for r in rows if r["status"] == "changed"),
        "extra": sum(1 for r in rows if r["status"] == "extra"),
        "severity": _worst(*(r["severity"] for r in rows)),
        "rows": rows,
    }


def column_summary(rows):
    return {
        "source": sum(1 for r in rows if r["source"]),
        "target": sum(1 for r in rows if r["target"]),
        "same": sum(1 for r in rows if r["status"] == "same"),
        "renamed": [{"source": r["name"], "target": r["target"]["name"], "notes": r["notes"], "severity": r["severity"]}
                    for r in rows if r["status"] == "renamed"],
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

    # Columns: a rename the data check verified counts as the same column, but only while
    # that check still describes the tables.
    current = saved if data is not None and not data["stale"] else None
    cols = column_summary(apply_renames(rows, current))
    view["columns"] = cols
    possible = [r for r in (current or {}).get("renames") or [] if r.get("verdict") != "verified"]

    # Keys and rules (primary, unique and foreign keys, defaults, checks).
    cons = constraint_summary(compare_constraints(src.get("constraints"), tgt.get("constraints")))
    view["constraints"] = cons

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
    view["checks"] = {"table": "ok", "columns": cols["severity"], "rows": rows_level, "data": data_level,
                      "constraints": cons["severity"] if cons else "none"}

    reasons = []
    if cols["missing"]:
        reasons.append(("problem", f"{len(cols['missing'])} column{'s' if len(cols['missing']) != 1 else ''} "
                                   f"missing in RDS: {_names(cols['missing'])}"))
    for r in possible:
        reasons.append(("review", f"Possible rename {r['source']} → {r['target']}: {r['reason']}"))
    for s in (current or {}).get("identity") or []:
        if s.get("behind"):
            reasons.append(("problem", f"Identity counter of {s['column']} in RDS is behind (next value "
                                       f"{_fmt(s['next_value'])}, {_fmt(s['highest'] if s['increment'] > 0 else s['lowest'])} already used): "
                                       "new inserts will fail"))
    if cons:
        lost_pk = [r for r in cons["rows"] if r["kind"] == "primary key" and r["severity"] == "problem"]
        for r in lost_pk:
            reasons.append(("problem", f"Primary key ({r['what']}) {r['note'] if r['status'] == 'changed' else 'not in RDS'}"))
        soft_cons = [r for r in cons["rows"] if r["severity"] == "review"]
        if soft_cons:
            kinds = {}
            for r in soft_cons:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            reasons.append(("review", f"{len(soft_cons)} key{'s' if len(soft_cons) != 1 else ''} or rule"
                                      f"{'s' if len(soft_cons) != 1 else ''} not kept in RDS: "
                                      + ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in kinds.items())))
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
    soft += [{"name": f"{c['source']} → {c['target']}", "notes": c["notes"][1:]}
             for c in cols["renamed"] if c["severity"] == "review"]
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
