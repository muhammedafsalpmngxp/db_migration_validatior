"""Deterministic comparisons: column alignment between two tables, row count rules."""
import re

_NON_ALNUM = re.compile(r"[^0-9a-z]")


def normalize(name):
    """ActivityCode, activity_code and 'Activity Code' all become 'activitycode'."""
    return _NON_ALNUM.sub("", name.lower())


# A column that became a key or code in the target: Supervisor -> supervisor_id,
# Account -> account_guid.
KEY_SUFFIXES = ("id", "guid", "uid", "code", "key")


def _inferred_names(source_name, target_table):
    """Target names the migration's naming convention would give `source_name`.

    Two patterns seen in AlTasnimBI: a key suffix (CrewType -> crew_type_id), and the
    table's name, or a leading part of it, as a prefix (id -> company_id on ref.company,
    Name -> emp_name on ref.employee).
    """
    base = normalize(source_name)
    if not base:
        return []
    names = [base + s for s in KEY_SUFFIXES]
    table = normalize(target_table or "")
    prefixes = {table[:n] for n in range(3, len(table) + 1)} if len(table) >= 3 else set()
    names += [p + base for p in sorted(prefixes, key=len, reverse=True)]
    return names


def align_columns(source_cols, target_cols, target_table=None, declared=None):
    """Pair source and target columns, then report what differs in each pair.

    Pairing runs in rounds, each only over columns still unpaired:
      declared    named in the mapping file (`columns:`), the DB team's word
      exact       identical name
      case        same name, different case
      normalized  same once case, underscores and spaces are ignored
      inferred    the target's naming convention (see `_inferred_names`) - a guess,
                  labelled as one

    Returns (rows, summary). Each row carries `source` and/or `target`, the round that
    paired it (`match`), its differences, and one `status`:
    match | renamed | changed | source_only | target_only.
    """
    remaining = {c["name"]: c for c in target_cols}
    pairs = {}

    def pair(src, tgt_name, how):
        pairs[src] = (remaining.pop(tgt_name), how)

    lower_to_target = {c["name"].lower(): c["name"] for c in target_cols}
    for src, tgt in (declared or {}).items():
        hit = lower_to_target.get(str(tgt).lower())
        if hit in remaining and any(c["name"] == src for c in source_cols):
            pair(src, hit, "declared")

    for col in source_cols:
        if col["name"] in pairs:
            continue
        hit = lower_to_target.get(col["name"].lower())
        if hit in remaining:
            pair(col["name"], hit, "exact" if hit == col["name"] else "case")

    def round_by(keys_for, how):
        by_norm = {}
        for name in remaining:
            by_norm.setdefault(normalize(name), []).append(name)
        for col in source_cols:
            if col["name"] in pairs:
                continue
            for key in keys_for(col["name"]):
                hit = next((n for n in by_norm.get(key, []) if n in remaining), None)
                if hit:
                    pair(col["name"], hit, how)
                    break

    round_by(lambda n: [normalize(n)], "normalized")
    round_by(lambda n: _inferred_names(n, target_table), "inferred")

    rows = []
    for col in source_cols:
        if col["name"] not in pairs:
            rows.append({"source": col, "target": None, "match": None, "diffs": [],
                         "status": "source_only"})
            continue
        tgt, how = pairs[col["name"]]
        diffs = []
        if col["type"].lower() != tgt["type"].lower():
            diffs.append("type")
        if col["nullable"] != tgt["nullable"]:
            diffs.append("nullable")
        if col["pk"] != tgt["pk"]:
            diffs.append("pk")
        if diffs:
            status = "changed"
        elif how not in ("exact", "case"):
            status = "renamed"
        else:
            status = "match"
        rows.append({"source": col, "target": tgt, "match": how, "diffs": diffs, "status": status})

    for tgt in target_cols:
        if tgt["name"] in remaining:
            rows.append({"source": None, "target": tgt, "match": None, "diffs": [],
                         "status": "target_only"})

    summary = {s: 0 for s in ("match", "renamed", "changed", "source_only", "target_only")}
    for r in rows:
        summary[r["status"]] += 1
    summary["type_changes"] = sum("type" in r["diffs"] for r in rows)
    summary["nullable_changes"] = sum("nullable" in r["diffs"] for r in rows)
    summary["source_columns"] = len(source_cols)
    summary["target_columns"] = len(target_cols)
    return rows, summary


def row_check(mapping, live_rows):
    """Does the target's live row count follow the mapping's rule?

    `live_rows` maps a TableRef key to its live count (None when the table is missing).
    """
    if mapping.type == "excluded":
        return {"rule": "Excluded from the migration: nothing to compare.", "expected": None,
                "actual": None, "delta": None, "delta_pct": None, "status": "excluded"}
    sources = [(m, live_rows.get(m.ref.key)) for m in mapping.sources]
    targets = [(m, live_rows.get(m.ref.key)) for m in mapping.targets]

    if mapping.type == "one_to_one":
        rule = "target rows = source rows"
        expected = sources[0][1]
    elif mapping.type == "union":
        rule = "target rows = sum of all source rows"
        expected = None if any(n is None for _, n in sources) else sum(n for _, n in sources)
    elif mapping.type == "merge":
        driving = next(m for m in mapping.sources if m.role == "driving")
        rule = f"target rows = driving table rows ({driving.ref.table})"
        expected = live_rows.get(driving.ref.key)
    else:
        return {"rule": "No count rule: business logic reshapes the rows.", "expected": None,
                "actual": None, "delta": None, "delta_pct": None, "status": "info"}

    actual = None if any(n is None for _, n in targets) else sum(n for _, n in targets)
    if expected is None or actual is None:
        return {"rule": rule, "expected": expected, "actual": actual, "delta": None,
                "delta_pct": None, "status": "unknown"}
    delta = actual - expected
    return {
        "rule": rule,
        "expected": expected,
        "actual": actual,
        "delta": delta,
        "delta_pct": round(delta * 100.0 / expected, 2) if expected else None,
        "status": "match" if delta == 0 else "mismatch",
    }

