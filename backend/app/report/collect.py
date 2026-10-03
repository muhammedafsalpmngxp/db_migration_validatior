"""The evidence of a report: what every check found, in one structure, graded by fixed rules.

Only results measured during this run (checked at or after `since`) count; anything older
is reported as not checked in this run, with what is known about it. Nothing here queries
table data: the checks have run already. The catalogs and row counts are read again (light),
which is also the final check for tables that changed during the run.

Section 1 is the ATNM copy check of the required tables (the source tables of the migration
plan); Section 2 is every mapping of the plan. No table is named here.
"""
import re
from datetime import datetime, timezone

from .. import config, coverage
from . import plain, settings


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fresh(ts, since):
    return bool(ts) and ts >= since


def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else ("—" if n is None else str(n))


# ---- Section 1: ATNM → RDS ------------------------------------------------------------------------

# A data migration is checked on its data: these kinds are table rules or settings, not data,
# so they are not graded or reported (the ATNM and RDS pages still show them).
RULE_KINDS = {"Primary key", "Keys & rules", "Identity"}


def _data_reasons(t, structure):
    """The reasons of a Section 1 table view that are about the data: without the table rules
    and without column differences other than the data type."""
    cols = t.get("columns") or {}
    soft = [c for c in cols.get("changed") or [] if c.get("severity") == "review"]
    soft += [{"name": f"{c['source']} → {c['target']}", "notes": c["notes"][1:]}
             for c in cols.get("renamed") or [] if c.get("severity") == "review"]
    soft_text = None
    if soft and not any(n.startswith("type ") for c in soft for n in c["notes"]):
        soft_text = structure._names([f"{c['name']}: {', '.join(c['notes'])}" for c in soft], 3)
    return [r for r in t.get("reasons") or []
            if _kind_of_reason(r["text"]) not in RULE_KINDS and r["text"] != soft_text]


def _kind_of_reason(text):
    t = text.lower()
    if t.startswith("not in rds") or "was not copied" in t or "neither database" in t:
        return "Table"
    if "rows differ" in t or "identical row" in t or "values differ" in t:
        return "Values"
    if "possible rename" in t:
        return "Renamed column"
    if "identity counter" in t:
        return "Identity"
    if "primary key" in t:
        return "Primary key"
    if "keys or rule" in t or "key or rule" in t:
        return "Keys & rules"
    if "type" in t and "changed" in t:
        return "Column type"
    if "only in rds" in t:
        return "Extra columns"
    if "missing in rds" in t:
        return "Columns"
    if "fewer rows" in t or "more rows" in t:
        return "Row count"
    if "empty" in t:
        return "Empty values"
    return "Other"


def part1(since, test=None):
    """Every required table of every ATNM database pair, graded (in a test run, `test`: only
    the picked ones, see testmode.py)."""
    from . import testmode
    from ..ATNM import api as atnm_api
    from ..ATNM import jobs as atnm_jobs
    from ..ATNM import required as atnm_required
    from ..ATNM import settings as atnm_settings
    from ..ATNM import structure

    src_label, tgt_label = atnm_settings.SOURCE.label, atnm_settings.TARGET.label
    out = {"items": [], "issues": [], "not_checked": [], "columns": [], "constraints": [], "empty": [],
           "renames": [], "row_diffs": [], "identity": [], "databases": [], "errors": []}
    pairs = atnm_settings.PAIRS
    cats = atnm_api._read_all(pairs, refresh=True)
    for p in pairs:
        src_cat, tgt_cat = cats[(p.id, "source")], cats[(p.id, "target")]
        try:
            req = atnm_required.tables(p)
            keep = testmode.keep1(test, p.id)
            if keep is not None:
                req = {k: v for k, v in req.items() if k in keep}
        except Exception as exc:
            out["errors"].append(f"The required tables of {p.source_db} cannot be read from the migration plan: {exc}")
            continue
        db = {"pair": p.id, "source_server": src_label, "source_db": p.source_db,
              "target_server": tgt_label, "target_db": p.target_db, "required": len(req)}
        out["databases"].append(db)
        view = atnm_api._pair_view(p, src_cat, tgt_cat)
        if view["errors"]:
            why = "; ".join(f"{e['label']}: {e['message']}" for e in view["errors"])
            out["errors"].append(f"{p.source_db} → {p.target_db}: {why}")
            for key, r in sorted(req.items()):
                item = _p1_item(p, src_label, tgt_label, {"schema": r["schema"], "table": r["table"]}, r["mapping"])
                item.update(result=plain.NOT_CHECKED, reason=f"The servers could not be read: {why}")
                out["items"].append(item)
                out["not_checked"].append(_nc(1, item, "Whole table", item["reason"],
                                              "Connect the VPN (ATNM) and generate the report again."))
            continue
        db["tables_source"] = sum(1 for t in view["tables"] if t["in_source"])
        db["not_required"] = sum(1 for t in view["tables"] if t["in_source"] and not t.get("required"))
        for t in view["tables"]:
            if t.get("required") and t["key"] in req:
                _p1_table(out, p, src_label, tgt_label, t, src_cat, tgt_cat, since, structure, atnm_jobs)
    return out


def _p1_item(p, src_label, tgt_label, t, mapping):
    return {"part": 1, "id": f"1|{p.id}|{t['schema']}.{t['table']}".lower(),
            "source_server": src_label, "source_db": p.source_db, "target_server": tgt_label,
            "target_db": p.target_db, "table": f"{t['schema']}.{t['table']}", "mapping": mapping,
            "in_source": None, "in_target": None, "rows_source": None, "rows_target": None, "rows_diff": None,
            "columns_source": None, "columns_target": None, "columns_missing": 0, "columns_extra": 0,
            "columns_changed": 0, "columns_renamed": 0, "keys_rules": "—", "values": "not checked",
            "values_detail": "", "checked_at": None, "live": False, "result": None, "reason": ""}


def _nc(part, item, what, why, todo):
    where = item["table"] if part == 1 else item["mapping"]
    return {"part": part, "database": item.get("source_db") or "", "item": where, "what": what,
            "reason": why, "todo": todo}


def _p1_table(out, p, src_label, tgt_label, t, src_cat, tgt_cat, since, structure, atnm_jobs):
    item = _p1_item(p, src_label, tgt_label, t, t.get("mapping"))
    saved = atnm_jobs.store.get(p.id, t["key"])
    fresh = bool(saved) and _fresh(saved.get("checked_at"), since) and saved.get("status") in ("identical", "different")
    data = t.get("data") or {}
    cols = t.get("columns") or {}
    rows = t.get("rows") or {}
    item.update(in_source=t["in_source"], in_target=t["in_target"], rows_source=rows.get("source"),
                rows_target=rows.get("target"), columns_source=cols.get("source"), columns_target=cols.get("target"),
                columns_missing=len(cols.get("missing") or []), columns_extra=len(cols.get("extra") or []),
                columns_changed=sum(1 for c in cols.get("changed") or [] if c.get("severity") == "problem"), columns_renamed=len(cols.get("renamed") or []),
                live=bool(t.get("live")), checked_at=(saved or {}).get("checked_at"))
    if item["rows_source"] is not None and item["rows_target"] is not None:
        item["rows_diff"] = item["rows_target"] - item["rows_source"]
    cons = t.get("constraints")
    item["keys_rules"] = ("could not be read" if cons is None else
                          "none" if not cons["rows"] else
                          "all kept" if not (cons["missing"] or cons["changed"]) else
                          f"{cons['missing'] + cons['changed']} not kept")
    if fresh and not data.get("stale"):
        item["values"] = {"identical": "identical", "different": "different"}[saved["status"]]
        item["values_detail"] = saved.get("headline") or ""
    elif saved and data.get("stale"):
        item["values"] = "changed since check"
        item["values_detail"] = data.get("stale_reason") or ""
    else:
        last = (saved or {}).get("last_attempt") or (saved if saved and saved.get("status") not in ("identical", "different") else None)
        item["values"] = "not checked"
        item["values_detail"] = (last or {}).get("headline") or "Not checked in this run."

    # The result: the table view's own grading, with values only from this run.
    status = t["status"]
    reasons = _data_reasons(t, structure)
    if t["in_source"] and t["in_target"]:
        level = ("problem" if any(r["severity"] == "problem" for r in reasons) else
                 "review" if any(r["severity"] == "review" for r in reasons) else "ok")
        if level != "ok":
            status = level
        elif (t.get("checks") or {}).get("data") == "ok":
            status = "verified"
        else:
            status = "unverified"
    if status == "problem":
        item["result"] = plain.MUST_FIX
    elif status == "review":
        item["result"] = plain.DECIDE
    elif status == "verified" and fresh:
        item["result"] = plain.CORRECT
    else:
        item["result"] = plain.NOT_CHECKED
    first = next((r["text"] for r in reasons if r["severity"] == status), None)
    item["reason"] = first or (t.get("reason") if t["status"] == status else "") or ""
    if item["result"] == plain.NOT_CHECKED and not item["reason"]:
        item["reason"] = "Values not checked in this run."
    if item["result"] == plain.CORRECT:
        item["reason"] = f"Identical: {item['values_detail']}" if item["values_detail"] else "Identical."
    if item["values"] in ("not checked", "changed since check") and t["in_source"] and t["in_target"]:
        why = item["values_detail"] or "Not checked in this run."
        out["not_checked"].append(_nc(1, item, "Values (row by row)", why,
                                      "Check again (a quiet moment, VPN connected)."))
        if item["result"] == plain.NOT_CHECKED:
            item["reason"] = f"Values not checked in this run: {why}"
    out["items"].append(item)

    for r in reasons:
        if r["severity"] not in ("problem", "review"):
            continue
        out["issues"].append(_issue(1, item, _kind_of_reason(r["text"]),
                                    plain.MUST_FIX if r["severity"] == "problem" else plain.DECIDE,
                                    r["text"], column=_column_of(r["text"])))
    if item["live"]:
        out["issues"].append(_issue(1, item, "Live table", plain.DECIDE,
                                    "The row count kept changing while the table was checked.", column=""))

    # Detail sheets.
    s_e, t_e = src_cat["tables"].get(t["key"]), tgt_cat["tables"].get(t["key"])
    if s_e and t_e:
        rows_c = structure.compare_columns(s_e["columns"], t_e["columns"])
        if fresh and not data.get("stale"):
            rows_c = structure.apply_renames(rows_c, saved)
        labels = {"missing": "Missing in RDS", "extra": "Only in RDS", "changed": "Changed", "renamed": "Renamed"}
        for c in rows_c:
            if c["status"] == "same":
                continue
            out["columns"].append({"part": 1, "database": p.source_db, "table": item["table"], "column": c["name"],
                                   "result": labels[c["status"]],
                                   "source_type": (c["source"] or {}).get("type", ""),
                                   "target_column": (c["target"] or {}).get("name", ""),
                                   "target_type": (c["target"] or {}).get("type", ""),
                                   "note": "; ".join(c["notes"])})
    if cons:
        for r in cons["rows"]:
            out["constraints"].append({"database": p.source_db, "table": item["table"], "kind": r["kind"],
                                       "what": r["what"], "status": r["status"], "note": r["note"],
                                       "result": {"problem": plain.MUST_FIX, "review": plain.DECIDE}.get(r["severity"], plain.CORRECT)})
    if fresh:
        prof = saved.get("profile") or {}
        for c in prof.get("columns") or []:
            s_, t_ = c.get("source"), c.get("target")
            out["empty"].append({"database": p.source_db, "table": item["table"], "column": c["name"],
                                 "rows_source": (prof.get("rows") or {}).get("source"),
                                 "rows_target": (prof.get("rows") or {}).get("target"),
                                 "null_source": s_["nulls"] if s_ else None, "blank_source": s_["blanks"] if s_ else None,
                                 "null_target": t_["nulls"] if t_ else None, "blank_target": t_["blanks"] if t_ else None})
        for r in saved.get("renames") or []:
            out["renames"].append({"part": 1, "database": p.source_db, "table": item["table"], **{
                k: r.get(k) for k in ("source", "target", "source_type", "target_type", "verdict", "paired",
                                      "identical", "rate", "reason")}})
        for s in saved.get("identity") or []:
            out["identity"].append({"part": 1, "database": p.target_db, "table": item["table"], **s})
        d, ex = saved.get("diff") or {}, saved.get("examples") or {}
        if saved["status"] == "different":
            out["row_diffs"].append({
                "database": p.source_db, "table": item["table"],
                "missing": d.get("missing", d.get("only_in_source")), "extra": d.get("extra", d.get("only_in_target")),
                "changed": d.get("changed"), "exact": not d.get("partial"),
                "missing_keys": ", ".join((ex.get("missing_keys") or [])[:settings.EXAMPLES_MAX]),
                "extra_keys": ", ".join((ex.get("extra_keys") or [])[:settings.EXAMPLES_MAX]),
                "changed_keys": ", ".join(f"{c['key']} ({', '.join(x['name'] for x in c['columns'])})"
                                          for c in (ex.get("changed") or [])[:settings.EXAMPLES_MAX]),
            })


_COLUMN_IN = re.compile(r"(?:missing in RDS|only in RDS|changed): (.+)$")


def _column_of(text):
    m = _COLUMN_IN.search(text)
    return m.group(1)[:200] if m else ""


def _issue(part, item, kind, result, finding, column=""):
    if part == 1:
        where = {"source_db": item["source_db"], "target_db": item["target_db"], "table": item["table"],
                 "target_table": item["table"], "mapping": item.get("mapping") or ""}
    else:
        where = {"source_db": item["source_db"], "target_db": item["target_db"], "table": item["source"],
                 "target_table": item["target"], "mapping": item["mapping"]}
    return {"part": part, "item": item["id"], **where, "column": column, "check": kind, "result": result,
            "finding": finding, "meaning": plain.meaning(kind), "action": plain.action(kind),
            "checked_at": item.get("checked_at")}


# ---- Section 2: RDS → AlTasnimBI ---------------------------------------------------------------------

DATA = {"identical": plain.CORRECT, "problems": plain.MUST_FIX, "review": plain.DECIDE}
KEYS = {"ok": plain.CORRECT, "none": plain.CORRECT, "problems": plain.MUST_FIX, "review": plain.DECIDE}
HEALTH = {"ok": plain.CORRECT, "problems": plain.MUST_FIX, "review": plain.DECIDE}
ROWS = {"match": plain.CORRECT, "mismatch": plain.MUST_FIX, "unknown": plain.MUST_FIX, "locked": plain.NOT_CHECKED,
        "info": plain.BY_DESIGN, "excluded": plain.BY_DESIGN}


def _db_name(side):
    return config.DATABASES[side]["name"]


def _kind_of_finding(text, default):
    t = text.lower()
    if "identity counter" in t:
        return "Identity"
    if "repeat another row" in t:
        return "Duplicates"
    if "point to a" in t and "does not exist" in t:
        return "Orphans"
    if "primary key" in t:
        return "Primary key"
    if "foreign key" in t:
        return "Keys & rules"
    if "values lost" in t or "more nulls" in t:
        return "Empty values"
    if "missing in the target" in t or "no source row" in t:
        return "Row count"
    return default


def part2(since, test=None):
    """Every mapping of the migration plan, graded (in a test run, `test`: only the picked
    ones, see testmode.py)."""
    from .. import main, quality, renames
    from . import testmode

    keep = testmode.keep2(test)

    plan = main.get_plan()
    live = main._live_tables(refresh=True)
    out = {"items": [], "issues": [], "not_checked": [], "columns": [], "renames": [], "values": [], "links": [],
           "health": [], "identity": [], "databases": [{"side": s, "name": d["name"], "role": d["role"]}
                                                       for s, d in config.DATABASES.items()]}
    for m in plan.mappings:
        if keep is not None and m.id not in keep:
            continue
        src_ref = m.sources[0].ref
        try:
            cmp_ = main.compare_table(src_ref.ref)
        except Exception as exc:
            cmp_ = {"row_check": {"status": "unknown", "rule": f"Could not be read: {exc}"}, "comparisons": []}
        data, km = main._checks.get(m.id) or {}, main._keymaps.get(m.id) or {}
        rn, q = renames.store.get(m.id) or {}, quality.store.get(m.id) or {}
        _p2_mapping(out, m, cmp_, data, km, rn, q, live, main, since)
    return out


def _p2_mapping(out, m, cmp_, data, km, rn, q, live, main, since):
    by_design = m.type in ("transform", "excluded")
    src_side, tgt_side = m.sources[0].ref.side, config.TARGET_SIDE
    item = {"part": 2, "id": f"2|{m.id}", "mapping": m.id, "type": m.type,
            "source_db": _db_name(src_side), "target_db": _db_name(tgt_side),
            "source": ", ".join(f"{s.ref.schema}.{s.ref.table}" for s in m.sources),
            "target": ", ".join(f"{t.ref.schema}.{t.ref.table}" for t in m.targets) or "—",
            "rule": None, "rows_expected": None, "rows_actual": None, "rows_diff": None,
            "row_count": None, "columns": None, "values": None, "key_links": None, "renames": None,
            "target_health": None, "checked_at": data.get("checked_at"), "result": None, "reason": ""}
    rc = cmp_.get("row_check") or {}
    item.update(rule=rc.get("rule"), rows_expected=rc.get("expected"), rows_actual=rc.get("actual"),
                rows_diff=rc.get("delta"))
    # Each table of the mapping with its own row count (a merge or union has several).
    mp = cmp_.get("mapping") or {}
    for key, members in (("source_tables", mp.get("sources")), ("target_tables", mp.get("targets"))):
        item[key] = [{"name": f"{x.get('database')} · {x.get('schema')}.{x.get('table')}", "rows": x.get("rows"),
                      "exists": x.get("exists", True)} for x in members or []]
    issues = []

    # Row count (read now, from the live tables).
    item["row_count"] = ROWS.get(rc.get("status"), plain.NOT_CHECKED)
    if rc.get("status") in ("mismatch", "unknown"):
        issues.append(("Row count", plain.MUST_FIX, f"{rc.get('rule')}: expected {_fmt(rc.get('expected'))}, found "
                                                     f"{_fmt(rc.get('actual'))} ({_fmt(rc.get('delta'))}).", ""))
    elif rc.get("status") == "locked":
        out["not_checked"].append(_nc(2, item, "Row count", rc.get("rule") or "Locked.", "Check again later."))

    def fresh(res):
        return bool(res) and _fresh(res.get("checked_at"), since)

    # What this run's data check measured: per-column verdicts and the empty-value profile.
    data_col = {(c.get("source") or "").lower(): c.get("verdict") for c in data.get("columns") or []} if fresh(data) else {}
    prof = (data.get("profile") or {}) if fresh(data) else {}
    empty_src = {c["column"].lower() for c in prof.get("source") or []
                 if prof.get("source_rows") and c["nulls"] + (c.get("blanks") or 0) >= prof["source_rows"]}
    empty_tgt = {c["column"].lower() for c in prof.get("target") or []
                 if prof.get("target_rows") and c["nulls"] + (c.get("blanks") or 0) >= prof["target_rows"]}
    item["notes"] = []

    # Source columns that feed a link the key check found by their values (Type feeds
    # project_type_id through project.project_type): moved, and checked row by row there.
    fed, fed_tgt = {}, set()
    if fresh(km):
        for lk in km.get("links") or []:
            if lk.get("found_by") == "values" and lk.get("source") and lk.get("column"):
                fed[lk["source"].lower()] = lk
                fed_tgt.add(lk["column"].lower())

    # Renames found by the data (this run only).
    verified_src, verified_tgt = {}, set()
    if fresh(rn) and rn.get("status") == "done":
        for t in rn.get("targets") or []:
            for d in t.get("decisions") or []:
                out["renames"].append({"part": 2, "mapping": m.id, "table": t.get("target", "").split(".", 1)[-1],
                                       **{k: d.get(k) for k in ("source", "target", "source_type", "target_type",
                                                                "verdict", "rows_checked", "identical", "rate",
                                                                "reason")}})
                if d["verdict"] == "verified":
                    verified_src[d["source"].lower()] = (d["target"], d.get("rows_checked"))
                    verified_tgt.add(d["target"].lower())
                elif d["source"].lower() not in fed:
                    issues.append(("Renamed column", plain.DECIDE,
                                   f"Possible rename {d['source']} → {d['target']}, not proven: {d.get('reason')}",
                                   f"{d['source']} → {d['target']}"))
            for n in t.get("named") or []:
                # The rename check compares the raw values; when the data check proved the pair
                # identical (converted, or translated through its list) there is nothing to decide.
                if data_col.get(n["source"].lower()) == "identical":
                    continue
                if n.get("verdict") not in ("verified", None) and (n.get("differing") or 0) > 0:
                    issues.append(("Renamed column", plain.DECIDE,
                                   f"{n['source']} and {n['target']} are paired by name, but the data does not agree: "
                                   f"{n.get('reason')}", f"{n['source']} → {n['target']}"))
        item["renames"] = f"{len(verified_src)} confirmed"
    elif not by_design:
        item["renames"] = "not checked"
        why = rn.get("headline") if rn else "Not checked in this run."
        out["not_checked"].append(_nc(2, item, "Renamed columns", why or "Not checked in this run.",
                                      "Generate the report again."))

    # Columns, with the renames proven by the data and the columns feeding a link counted as moved.
    dropped, retyped, dropped_empty, new_empty = [], [], [], []
    for comp in cmp_.get("comparisons") or []:
        tname = comp["target"].split(".", 1)[-1]
        for r in comp.get("rows") or []:
            s, t, st = r.get("source"), r.get("target"), r["status"]
            label, note = {"match": "Same", "renamed": "Renamed (name rules)", "changed": "Changed",
                           "source_only": "Not migrated", "target_only": "Only in target"}[st], ", ".join(r.get("diffs") or [])
            if st == "changed" and "type" not in (r.get("diffs") or []):
                label = "Same"       # only a setting differs (key, can be empty, ...), not the data type
            if st == "source_only" and s["name"].lower() in verified_src:
                tgt_name, n = verified_src[s["name"].lower()]
                label, note, t = "Renamed (proven by data)", f"identical on all {_fmt(n)} paired rows", {"name": tgt_name, "type": ""}
            elif st == "source_only" and s["name"].lower() in fed:
                lk = fed[s["name"].lower()]
                ref = f"{(lk.get('ref') or {}).get('schema')}.{(lk.get('ref') or {}).get('table')}"
                b = lk.get("buckets") or {}
                with_value = sum(v or 0 for v in b.values()) - (b.get("both_empty") or 0)
                label, t = "Moved into a link", {"name": lk["column"], "type": ""}
                note = (f"kept as the id {lk['column']} of {ref}: {_fmt(b.get('correct') or 0)} of {_fmt(with_value)} "
                        "records with a value point at the right row")
                item["notes"].append(f"{s['name']} → {lk['column']}: {note} (see the key links).")
            elif st == "target_only" and (t["name"].lower() in verified_tgt or t["name"].lower() in fed_tgt):
                continue
            elif st == "source_only" and not by_design:
                if s["name"].lower() in empty_src:
                    dropped_empty.append(s["name"])
                else:
                    dropped.append(s["name"] + (f" (paired by name with {r['released']}, which is empty on every row)"
                                                if r.get("released") else ""))
            elif st == "target_only" and t["name"].lower() in empty_tgt:
                new_empty.append(t["name"])
            elif st == "changed" and "type" in (r.get("diffs") or []) and data_col.get(s["name"].lower()) not in (None, "identical"):
                retyped.append(s["name"])
            out["columns"].append({"part": 2, "mapping": m.id, "table": tname, "column": s["name"] if s else "",
                                   "source_type": (s or {}).get("type", ""), "target_column": (t or {}).get("name", ""),
                                   "target_type": (t or {}).get("type", ""), "result": label, "note": note})
    if dropped:
        issues.append(("Column not migrated", plain.DECIDE,
                       f"{len(dropped)} column{'s' if len(dropped) != 1 else ''} of the source table "
                       f"{'are' if len(dropped) != 1 else 'is'} not in the target table, and no renamed column holds "
                       f"{'their' if len(dropped) != 1 else 'its'} data: {_names(dropped)}.", _names(dropped, 6)))
    for name in retyped:
        issues.append(("Column type", plain.DECIDE, f"{name}: the data type changed and the values differ.", name))
    if dropped_empty:
        item["notes"].append(f"{_names(dropped_empty, 8)} not moved to the target table, but "
                             f"{'they were' if len(dropped_empty) != 1 else 'it was'} empty on every source row: "
                             "no data is lost.")
    if new_empty:
        item["notes"].append(f"{len(new_empty)} target column{'s are' if len(new_empty) != 1 else ' is'} empty on every "
                             f"row: {_names(new_empty, 8)}. Confirm the application does not read "
                             f"{'them' if len(new_empty) != 1 else 'it'}.")
    item["columns"] = plain.DECIDE if (dropped or retyped) else (plain.BY_DESIGN if by_design else plain.CORRECT)

    # Values, key links and target table health: this run's results only.
    def component(res, table, name, what):
        if by_design and name != "health":
            return plain.BY_DESIGN
        if not fresh(res):
            why = (res.get("headline") if res else None) or "Not checked in this run."
            out["not_checked"].append(_nc(2, item, what, why, "Generate the report again."))
            return plain.NOT_CHECKED
        st = res.get("status")
        if st in table:
            return table[st]
        out["not_checked"].append(_nc(2, item, what, res.get("headline") or st, "Check again later."))
        return plain.NOT_CHECKED

    item["values"] = component(data, DATA, "values", "Values (row by row)")
    item["key_links"] = component(km, KEYS, "keys", "Key links")
    item["target_health"] = component(q, HEALTH, "health", "Target table health") if m.targets else plain.BY_DESIGN

    # Changed after its check (the final row count read): the values are a snapshot.
    rows_then = (data.get("rows") or {}) if fresh(data) else {}
    if item["values"] in (plain.CORRECT, plain.MUST_FIX, plain.DECIDE) and rows_then.get("target") is not None \
            and rc.get("actual") is not None and rows_then.get("target") != rc.get("actual"):
        issues.append(("Live table", plain.DECIDE, f"The target table changed during the run ({_fmt(rows_then.get('target'))} "
                                                   f"rows when checked, {_fmt(rc.get('actual'))} now).", ""))

    if fresh(data):
        item["values_detail"] = data.get("headline") or ""
        for f in data.get("findings") or []:
            if f["severity"] in ("error", "review") and _kind_of_finding(f["text"], "Values") not in RULE_KINDS:
                issues.append((_kind_of_finding(f["text"], "Values"),
                               plain.MUST_FIX if f["severity"] == "error" else plain.DECIDE, f["text"], f.get("column") or ""))
        for c in data.get("columns") or []:
            b = c.get("buckets") or {}
            n = c.get("nulls") or {}
            out["values"].append({"mapping": m.id, "column": c.get("label"), "verdict": c.get("verdict"),
                                  **{k: b.get(k) for k in ("identical", "different", "lost", "added", "recoded",
                                                           "case_only", "blank_to_null")},
                                  "null_source": n.get("source"), "null_target": n.get("target")})
        for c in data.get("large_columns") or []:
            out["values"].append({"mapping": m.id, "column": f"{c['source']} ({c['stype']}, by content)",
                                  "verdict": c.get("verdict"), "identical": c.get("identical"),
                                  "different": c.get("different", c.get("only_in_source")), "lost": c.get("lost"),
                                  "added": c.get("added")})
        for nc in data.get("not_compared") or []:
            out["not_checked"].append(_nc(2, item, f"Column {nc['column']} ({nc['type']})", nc["reason"],
                                          "Compare it by hand if it matters."))
    if fresh(km):
        for f in km.get("findings") or []:
            if f["severity"] in ("error", "review"):
                issues.append(("Key links", plain.MUST_FIX if f["severity"] == "error" else plain.DECIDE, f["text"],
                               f.get("column") or ""))
        for lk in km.get("links") or []:
            b = lk.get("buckets") or {}
            out["links"].append({"mapping": m.id, "column": lk.get("column"),
                                 "points_to": f"{(lk.get('ref') or {}).get('schema')}.{(lk.get('ref') or {}).get('table')}",
                                 **{k: b.get(k) for k in ("correct", "not_in_list", "wrong", "not_filled", "orphan")},
                                 "verdict": lk.get("verdict"), "summary": lk.get("summary") or ""})
    if fresh(q):
        kept = [f for f in q.get("findings") or [] if f["severity"] in ("error", "review")
                and _kind_of_finding(f["text"], "Keys & rules") not in RULE_KINDS]
        for f in kept:
            issues.append((_kind_of_finding(f["text"], "Keys & rules"),
                           plain.MUST_FIX if f["severity"] == "error" else plain.DECIDE, f["text"], ""))
        if item["target_health"] in (plain.MUST_FIX, plain.DECIDE):
            item["target_health"] = (plain.MUST_FIX if any(f["severity"] == "error" for f in kept) else
                                     plain.DECIDE if kept else plain.CORRECT)
        for t in q.get("tables") or []:
            if not t.get("exists"):
                continue
            dup, sdup = t.get("duplicates") or {}, t.get("source_duplicates") or {}
            fks = t.get("foreign_keys") or []
            out["health"].append({
                "mapping": m.id, "table": t["table"], "rows": t.get("rows"),
                "primary_key": ", ".join(t.get("primary_key") or []) or "none",
                "source_primary_key": ", ".join(t.get("source_primary_key") or []) or ("none" if "source_primary_key" in t else "—"),
                "duplicate_rows": dup.get("duplicate_rows"), "source_duplicate_rows": sdup.get("duplicate_rows"),
                "foreign_keys": len(fks), "not_enforced": sum(1 for f in fks if not (f["enabled"] and f["trusted"])),
                "orphan_rows": sum(f.get("orphans") or 0 for f in fks)})
            for s in t.get("identity") or []:
                out["identity"].append({"part": 2, "database": item["target_db"], "table": t["table"], **s})

    # The mapping's result.
    parts = [item["row_count"], item["columns"], item["values"], item["key_links"], item["target_health"]]
    parts += [r for _, r, _, _ in issues]
    if by_design:
        item["result"] = plain.worst(item["target_health"], *[r for k, r, _, _ in issues if k != "Live table"])
        if item["result"] in (plain.CORRECT, plain.BY_DESIGN, plain.NOT_CHECKED) and item["target_health"] != plain.MUST_FIX:
            item["result"] = plain.BY_DESIGN
    else:
        item["result"] = plain.worst(*parts)
    issues = _merge_by_column(issues)
    first = next((f for k, r, f, _ in issues if r == item["result"]), None)
    item["reason"] = (first or (cmp_.get("mapping") or {}).get("note") or item.get("values_detail")
                      or ("Excluded from the migration." if m.type == "excluded" else "")
                      or ("Business logic reshapes the rows, so they are not compared one to one." if by_design else ""))
    if item["result"] == plain.CORRECT:
        item["reason"] = item.get("values_detail") or "Row count, columns, values and links are correct."
    if item["result"] == plain.NOT_CHECKED and not first:
        nc = [x for x in out["not_checked"] if x["item"] == m.id]
        item["reason"] = f"{nc[0]['what']} not checked: {nc[0]['reason']}" if nc else "Not checked in this run."
    out["items"].append(item)
    for kind, result, finding, column in issues:
        out["issues"].append(_issue(2, item, kind, result, finding, column=column))


def _column_key(column):
    """The source column an issue is about (lower case), or None for several columns."""
    if not column or "," in column:
        return None
    return column.split("→")[0].strip().lower() or None


def _merge_by_column(issues):
    """One issue per source column: what the checks found about the same column (a rename
    the data does not support, a changed type, recoded or lost values) is one line, graded
    by its worst part. A row-level line that only says the rows differ in those columns is
    dropped when every column it names has its own line. Order is kept."""
    groups, order = {}, []
    for it in issues:
        key = _column_key(it[3])
        if key is None:
            order.append([it])
            continue
        if key not in groups:
            groups[key] = []
            order.append(groups[key])
        groups[key].append(it)
    out = []
    for group in order:
        if len(group) == 1:
            out.append(group[0])
            continue
        worst = min(group, key=lambda it: plain.ORDER[it[1]])
        label = next((it[3] for it in group if "→" in it[3]), group[0][3])
        prefixes = sorted({label} | {it[3] for it in group}, key=len, reverse=True)
        parts = []
        for _, _, finding, _ in group:
            text = finding
            for pfx in prefixes:
                if text.startswith(pfx + ":"):
                    text = text[len(pfx) + 1:].strip()
                    break
            if text not in parts:
                parts.append(text)
        out.append((worst[0], worst[1], f"{label}: " + " · ".join(parts), label))
    own = {_column_key(it[3]) for it in out if _column_key(it[3])}
    kept = []
    for it in out:
        text = it[2]
        if not it[3] and "column" in text and "to review:" in text:
            named = [x.strip().rstrip(".") for x in text.split("to review:", 1)[1].split(",")]
            if named and all(_column_key(x) in own for x in named):
                continue
        kept.append(it)
    return kept


def _names(items, n=12):
    items = list(items)
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


# ---- everything --------------------------------------------------------------------------------------

def build(run, since):
    """The whole evidence of a run: both parts, coverage, totals and the overall result."""
    p1, p2 = part1(since, run.get("test")), part2(since, run.get("test"))
    from .. import main
    try:
        cov = coverage.report(main.get_plan())
    except Exception as exc:
        cov = {"databases": [], "error": str(exc)}
    issues = sorted(p1["issues"] + p2["issues"],
                    key=lambda i: (plain.ORDER[i["result"]], i["part"], i["source_db"], i["table"]))
    for n, i in enumerate(issues, 1):
        i["id"] = f"{i['part']}-{n:03d}"
    items = p1["items"] + p2["items"]

    def tally(rows):
        out = {r: 0 for r in plain.RESULTS}
        for x in rows:
            out[x["result"]] += 1
        return out

    ev = {
        "run": {**run, "collected_at": _now()},
        "overall": plain.overall(x["result"] for x in items),
        "totals": {"part1": tally(p1["items"]), "part2": tally(p2["items"]),
                   "issues": {r: sum(1 for i in issues if i["result"] == r) for r in plain.RESULTS}},
        "part1": {k: v for k, v in p1.items() if k not in ("issues", "not_checked")},
        "part2": {k: v for k, v in p2.items() if k not in ("issues", "not_checked")},
        "issues": issues,
        "not_checked": p1["not_checked"] + p2["not_checked"],
        "coverage": cov,
    }
    return ev
