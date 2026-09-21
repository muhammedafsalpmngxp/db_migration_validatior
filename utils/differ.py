"""Deterministic diff logic.

The LLM decides *what* to compare and explains the result; these pure functions decide
*whether* two things actually differ, so nothing in the report rests on a guess.
"""


# ---------------------------------------------------------------- schema


def diff_schemas(schema_a, schema_b):
    tables_a, tables_b = set(schema_a), set(schema_b)
    common = sorted(tables_a & tables_b)

    table_diffs = {}
    for t in common:
        a, b = schema_a[t], schema_b[t]
        cols_a, cols_b = a["columns"], b["columns"]
        only_a = sorted(set(cols_a) - set(cols_b))
        only_b = sorted(set(cols_b) - set(cols_a))

        type_changes, nullability_changes, default_changes = [], [], []
        for c in sorted(set(cols_a) & set(cols_b)):
            ca, cb = cols_a[c], cols_b[c]
            if ca["type"] != cb["type"]:
                type_changes.append({"column": c, "a": ca["type"], "b": cb["type"]})
            if ca["nullable"] != cb["nullable"]:
                nullability_changes.append(
                    {
                        "column": c,
                        "a": "NULL" if ca["nullable"] else "NOT NULL",
                        "b": "NULL" if cb["nullable"] else "NOT NULL",
                    }
                )
            if (ca["default"] or None) != (cb["default"] or None):
                default_changes.append({"column": c, "a": ca["default"], "b": cb["default"]})

        pk_changed = a["primary_key"] != b["primary_key"]
        idx_a = {i["name"] for i in a["indexes"]}
        idx_b = {i["name"] for i in b["indexes"]}

        diff = {
            "columns_only_in_a": only_a,
            "columns_only_in_b": only_b,
            "type_changes": type_changes,
            "nullability_changes": nullability_changes,
            "default_changes": default_changes,
            "primary_key_a": a["primary_key"],
            "primary_key_b": b["primary_key"],
            "primary_key_changed": pk_changed,
            "indexes_only_in_a": sorted(idx_a - idx_b),
            "indexes_only_in_b": sorted(idx_b - idx_a),
            "row_count_a": a["row_count"],
            "row_count_b": b["row_count"],
        }
        diff["identical"] = not any(
            [
                only_a,
                only_b,
                type_changes,
                nullability_changes,
                default_changes,
                pk_changed,
                diff["indexes_only_in_a"],
                diff["indexes_only_in_b"],
            ]
        )
        table_diffs[t] = diff

    return {
        "tables_only_in_a": sorted(tables_a - tables_b),
        "tables_only_in_b": sorted(tables_b - tables_a),
        "common_tables": common,
        "table_diffs": table_diffs,
        "tables_with_schema_changes": [t for t in common if not table_diffs[t]["identical"]],
        "row_count_mismatches": [
            {
                "table": t,
                "a": table_diffs[t]["row_count_a"],
                "b": table_diffs[t]["row_count_b"],
            }
            for t in common
            if table_diffs[t]["row_count_a"] != table_diffs[t]["row_count_b"]
        ],
    }


# ---------------------------------------------------------------- rows


def _norm(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _equal(x, y):
    x, y = _norm(x), _norm(y)
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return abs(float(x) - float(y)) < 1e-9
    return x == y


def _key(row, pk_cols):
    return tuple(_norm(row.get(c)) for c in pk_cols)


def _sortkey(key):
    """Order numeric keys numerically and everything else as text."""
    return tuple((0, float(v), "") if isinstance(v, (int, float)) else (1, 0.0, str(v))
                 for v in key)


def diff_rows(rows_a, rows_b, pk_cols, compare_columns=None, max_examples=50):
    """Key based row diff: rows missing on either side plus per column value changes."""
    if not pk_cols:
        raise ValueError("A primary key is required for a row level comparison.")

    index_a = {_key(r, pk_cols): r for r in rows_a}
    index_b = {_key(r, pk_cols): r for r in rows_b}

    keys_a, keys_b = set(index_a), set(index_b)
    only_a = sorted(keys_a - keys_b, key=_sortkey)
    only_b = sorted(keys_b - keys_a, key=_sortkey)

    shared_cols = set(rows_a[0]) & set(rows_b[0]) if rows_a and rows_b else set()
    cols = [c for c in (compare_columns or sorted(shared_cols)) if c in shared_cols]

    modified = []
    for k in sorted(keys_a & keys_b, key=_sortkey):
        ra, rb = index_a[k], index_b[k]
        changes = {
            c: {"a": ra.get(c), "b": rb.get(c)}
            for c in cols
            if not _equal(ra.get(c), rb.get(c))
        }
        if changes:
            modified.append({"key": dict(zip(pk_cols, k)), "changes": changes})

    return {
        "primary_key": pk_cols,
        "compared_columns": cols,
        "rows_a": len(rows_a),
        "rows_b": len(rows_b),
        "matching_rows": len(keys_a & keys_b) - len(modified),
        "only_in_a_count": len(only_a),
        "only_in_b_count": len(only_b),
        "modified_count": len(modified),
        "only_in_a": [dict(zip(pk_cols, k)) for k in only_a[:max_examples]],
        "only_in_b": [dict(zip(pk_cols, k)) for k in only_b[:max_examples]],
        "modified": modified[:max_examples],
        "identical": not (only_a or only_b or modified),
    }


# ---------------------------------------------------------------- summaries


def summarize_schema_diff(diff, label_a="A", label_b="B", max_tables=30):
    """Compact text the LLM can read without burning the context window."""
    lines = []
    lines.append(f"Tables only in {label_a}: {', '.join(diff['tables_only_in_a']) or 'none'}")
    lines.append(f"Tables only in {label_b}: {', '.join(diff['tables_only_in_b']) or 'none'}")
    lines.append(f"Common tables: {', '.join(diff['common_tables']) or 'none'}")
    for t in diff["tables_with_schema_changes"][:max_tables]:
        d = diff["table_diffs"][t]
        parts = []
        if d["columns_only_in_a"]:
            parts.append(f"columns only in {label_a}: {', '.join(d['columns_only_in_a'])}")
        if d["columns_only_in_b"]:
            parts.append(f"columns only in {label_b}: {', '.join(d['columns_only_in_b'])}")
        for tc in d["type_changes"]:
            parts.append(f"{tc['column']} type {tc['a']} -> {tc['b']}")
        for nc in d["nullability_changes"]:
            parts.append(f"{nc['column']} {nc['a']} -> {nc['b']}")
        for dc in d["default_changes"]:
            parts.append(f"{dc['column']} default {dc['a']} -> {dc['b']}")
        if d["primary_key_changed"]:
            parts.append(f"primary key {d['primary_key_a']} -> {d['primary_key_b']}")
        if d["indexes_only_in_a"]:
            parts.append(f"indexes only in {label_a}: {', '.join(d['indexes_only_in_a'])}")
        if d["indexes_only_in_b"]:
            parts.append(f"indexes only in {label_b}: {', '.join(d['indexes_only_in_b'])}")
        lines.append(f"- {t}: " + "; ".join(parts))
    for rc in diff["row_count_mismatches"]:
        lines.append(f"- {rc['table']}: row count {rc['a']} vs {rc['b']}")
    return "\n".join(lines)
