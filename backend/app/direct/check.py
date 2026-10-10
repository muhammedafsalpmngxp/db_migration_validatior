"""One mapping of the plan, checked from the catalogs of the client's ATNM databases and the
new system (AlTasnimBI) - no table data is read.

A plan table on side A or B is the RDS copy of a client table; here it is resolved to the
client's original: the ATNM database whose copy is that side's database (the ATNM_DB_<n>
pairs), with the name under `atnm_names` when the copy was renamed. A table on side T is
in AlTasnimBI on the RDS server.

Per mapping: do the tables exist, do the record counts follow the plan's rule (one to one,
sum of a union, the driving table of a merge), and how do the columns pair up (the same
pairing rules as the RDS section: declared in the plan, by name, by naming convention).
"""
from .. import compare, config
from ..ATNM import settings as atnm_settings
from ..report import plain


def target_db():
    return config.DATABASES[config.TARGET_SIDE]["name"]


def pair_of(side):
    """The ATNM database pair whose RDS copy is side A's or B's database, or None."""
    rds_db = config.DATABASES[side]["name"].lower()
    return next((p for p in atnm_settings.PAIRS if p.target_db.lower() == rds_db), None)


def resolve(ref, plan):
    """(server key, database, schema.table) of a plan table: the client's original on ATNM for
    side A/B, AlTasnimBI for side T. None when no ATNM pair is set for the side."""
    if ref.side == config.TARGET_SIDE:
        return "target", target_db(), f"{ref.schema}.{ref.table}"
    p = pair_of(ref.side)
    if p is None:
        return None
    return "source", p.source_db, plan.atnm_names.get(ref.key) or f"{ref.schema}.{ref.table}"


def databases(plan):
    """[(server key, database)] the check reads: each ATNM database the plan uses, then AlTasnimBI."""
    out = []
    for side in config.SOURCE_SIDES:
        p = pair_of(side)
        if p and any(s.ref.side == side for m in plan.mappings for s in m.sources):
            out.append(("source", p.source_db))
    out.append(("target", target_db()))
    return out


def _entry(cats, where):
    """The catalog entry of a resolved table, or None (missing, or its database unreadable)."""
    if where is None:
        return None
    cat = cats.get((where[0], where[1].lower()))
    if not cat or "error" in cat:
        return None
    return cat["tables"].get(where[2].lower())


def _cols(entry):
    """Catalog columns in the shape compare.align_columns takes."""
    pk = {c.lower() for c in ((entry.get("constraints") or {}).get("primary_key") or [])}
    if not pk and (entry.get("row_key") or {}).get("kind") == "primary key":
        pk = {c.lower() for c in entry["row_key"]["columns"]}
    return [{"name": c["name"], "type": c["type"], "nullable": c["nullable"], "identity": c["identity"],
             "pk": c["name"].lower() in pk} for c in entry["columns"] if not c.get("computed")]


def _fk_columns(entry):
    return {c.lower() for fk in ((entry.get("constraints") or {}).get("foreign_keys") or []) for c in fk["columns"]}


def _member(mem, plan, cats, labels):
    where = resolve(mem.ref, plan)
    entry = _entry(cats, where)
    unread = where is not None and "error" in (cats.get((where[0], where[1].lower())) or {"error": "not read"})
    return {
        "ref": mem.ref.ref, "role": mem.role, "side": mem.ref.side,
        "server": labels["source" if mem.ref.side != config.TARGET_SIDE else "target"],
        "database": where[1] if where else None,
        "table": f"{entry['schema']}.{entry['table']}" if entry else (where[2] if where else mem.ref.ref),
        "exists": None if unread or where is None else entry is not None,
        "rows": entry["rows"] if entry else None,
        "columns": len(entry["columns"]) if entry else None,
        "_entry": entry,
    }


def _issue(m, kind, result, finding, column=""):
    return {"mapping": m.id, "check": kind, "result": result, "finding": finding, "column": column,
            "meaning": plain.meaning(kind), "action": plain.action(kind)}


def evaluate(m, plan, cats, labels):
    """The result of one mapping: its tables, record counts, column pairing, issues and grade."""
    sources = [_member(s, plan, cats, labels) for s in m.sources]
    targets = [_member(t, plan, cats, labels) for t in m.targets]
    issues, notes, not_checked = [], [], []
    item = {"mapping": m.id, "type": m.type, "note": m.note, "sources": sources, "targets": targets,
            "rule": None, "expected": None, "actual": None, "difference": None,
            "columns": None, "column_rows": [], "issues": issues, "notes": notes, "not_checked": not_checked,
            "result": None}

    if m.type == "excluded":
        item["result"] = plain.BY_DESIGN
        notes.append("Excluded from the migration: nothing to compare.")
        _strip(item)
        return item

    # Tables
    for x in sources + targets:
        where = "the client server (ATNM)" if x["side"] != config.TARGET_SIDE else f"the new system ({target_db()})"
        if x["exists"] is False:
            issues.append(_issue(m, "Table", plain.MUST_FIX, f"{x['table']} is not in {x['database']} on {where}."))
        elif x["exists"] is None:
            not_checked.append({"what": x["table"], "reason": f"{x['database'] or x['ref']} on {where} could not be "
                                                              "read in this run.",
                                "todo": "Check the connection (VPN for ATNM) and run the report again."})

    # Record counts, by the plan's rule
    live = {mem.ref.key: x["rows"] for mem, x in zip(m.sources + m.targets, sources + targets)}
    rc = compare.row_check(m, live)
    item.update(rule=rc["rule"], expected=rc["expected"], actual=rc["actual"], difference=rc["delta"])
    if rc["status"] == "mismatch":
        d = rc["delta"]
        issues.append(_issue(m, "Row count", plain.MUST_FIX,
                             f"{rc['rule']}: expected {rc['expected']:,}, found {rc['actual']:,} "
                             f"({abs(d):,} {'more' if d > 0 else 'fewer'} in the new system)."))

    # Columns: each client table that feeds rows (all of a union, the driving one of a merge,
    # the one of a one to one) against the target
    if m.type == "transform":
        notes.append("Transform: business logic reshapes the rows, so they are not compared one to one.")
    elif targets and targets[0]["_entry"]:
        tgt = targets[0]["_entry"]
        feeding = sources if m.type == "union" else (
            [x for x, mem in zip(sources, m.sources) if mem.role == "driving"] if m.type == "merge" else sources[:1])
        summary_total = {}
        for x in feeding:
            if not x["_entry"]:
                continue
            rows, summary = compare.align_columns(_cols(x["_entry"]), _cols(tgt), target_table=tgt["table"],
                                                  declared=m.columns, fk_columns=_fk_columns(tgt))
            for k, v in summary.items():
                summary_total[k] = summary_total.get(k, 0) + v
            label = f" (from {x['table']})" if len(feeding) > 1 else ""
            for r in rows:
                s, t = r["source"], r["target"]
                item["column_rows"].append({
                    "source_table": x["table"], "source": s["name"] if s else None, "source_type": s["type"] if s else None,
                    "target": t["name"] if t else None, "target_type": t["type"] if t else None,
                    "status": r["status"], "match": r["match"], "diffs": r["diffs"]})
            lost = [r["source"]["name"] for r in rows if r["status"] == "source_only"]
            if lost:
                issues.append(_issue(m, "Column not migrated", plain.DECIDE,
                                     f"{len(lost)} column{'s' if len(lost) != 1 else ''} of the client table{label} "
                                     f"{'have' if len(lost) != 1 else 'has'} no column in the new table: "
                                     f"{_names(lost)}.", ", ".join(lost[:10])))
            typed = [f"{r['source']['name']} ({r['source']['type']} → {r['target']['type']})" for r in rows
                     if "type" in r["diffs"]]
            if typed:
                notes.append(f"Type changed{label}: {_names(typed)} - the values are not compared in this version.")
            guessed = [f"{r['source']['name']} → {r['target']['name']}" for r in rows if r["match"] == "inferred"]
            if guessed:
                notes.append(f"Paired by naming convention{label} (a guess): {_names(guessed)}.")
        if summary_total:
            item["columns"] = summary_total
        new_cols = sorted({r["target"] for r in item["column_rows"] if r["status"] == "target_only"})
        if new_cols:
            notes.append(f"New columns in the target: {_names(new_cols)}.")
        lookups = [x["table"] for x, mem in zip(sources, m.sources) if m.type == "merge" and mem.role != "driving"]
        if lookups:
            notes.append(f"Merge: {_names(lookups)} only adds values to the rows of the driving table.")

    if issues:
        item["result"] = plain.worst(*(i["result"] for i in issues))
    elif not_checked or rc["status"] == "unknown" and m.type != "transform":
        item["result"] = plain.NOT_CHECKED
        if not not_checked:
            not_checked.append({"what": "Record count", "reason": "A record count could not be read.",
                                "todo": "Run the report again."})
    elif m.type == "transform":
        item["result"] = plain.BY_DESIGN
    else:
        item["result"] = plain.CORRECT
    _strip(item)
    return item


def _names(names, n=8):
    names = list(names)
    return ", ".join(names[:n]) + (f" and {len(names) - n} more" if len(names) > n else "")


def _strip(item):
    for x in item["sources"] + item["targets"]:
        x.pop("_entry", None)


def table_count(plan, cats, labels):
    """Each database read: its tables, and how many of them the plan uses."""
    used = {}
    for m in plan.mappings:
        for mem in m.sources + m.targets:
            where = resolve(mem.ref, plan)
            if where:
                used.setdefault((where[0], where[1].lower()), set()).add(where[2].lower())
    out = []
    for (server, db) in databases(plan):
        cat = cats.get((server, db.lower())) or {}
        mine = used.get((server, db.lower()), set())
        if "error" in cat or "tables" not in cat:
            out.append({"server": labels[server], "database": db, "tables": None, "used": None, "not_used": [],
                        "error": cat.get("error", "not read")})
            continue
        others = sorted((t for k, t in cat["tables"].items() if k not in mine), key=lambda t: t["key"])
        out.append({"server": labels[server], "database": db, "tables": len(cat["tables"]),
                    "used": len(cat["tables"]) - len(others),
                    "not_used": [{"table": f"{t['schema']}.{t['table']}", "rows": t["rows"]} for t in others],
                    "error": None})
    return out
