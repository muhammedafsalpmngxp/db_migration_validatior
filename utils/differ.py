"""Deterministic diff logic.

The LLM decides *what* to compare and explains the result; these pure functions decide
*whether* two things actually differ, so nothing in the report rests on a guess.
"""


# ---------------------------------------------------------------- pairing


def normalize(name):
    """Fold a table name to what two teams would call the same thing.

    `PlantDescription`, `plant_description` and `PLANT DESCRIPTION` all normalise to
    `plantdescription`, which is enough to match most renames between two databases
    without guessing.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def match_tables(schema_a, schema_b, chosen_a=None, chosen_b=None, extra_pairs=None):
    """Work out which table on A is which table on B.

    Matching is schema-agnostic in both directions: `dbo.x` pairs with `well.x`, `ref.x`
    with `core.x`, whatever schema either side keeps it in. Three passes, most certain
    first:

      exact       same table name
      normalized  same name ignoring case, underscores and spaces (PlantDescription)
      given       a pair supplied by the caller - the agent's proposal, or the user's
                  own pick in the UI

    A name that occurs in several schemas of the *same* database is ambiguous and is left
    unmatched, unless the user picked one of them (`chosen_a` / `chosen_b`), which is the
    answer to that ambiguity.

    Returns (pairs, unmatched_a, unmatched_b); each pair is
    {"a": qualified, "b": qualified, "key": name, "method": ...}.
    """
    chosen_a = {c for c in (chosen_a or []) if "." in c}
    chosen_b = {c for c in (chosen_b or []) if "." in c}

    def index(schema, chosen, keyfunc):
        """value -> the one qualified name that owns it here, or None when ambiguous."""
        groups = {}
        for qualified, entry in schema.items():
            groups.setdefault(keyfunc(entry["table"]), []).append(qualified)
        out = {}
        for value, qualifieds in groups.items():
            picked = [q for q in qualifieds if q in chosen]
            candidates = picked or qualifieds
            out[value] = candidates[0] if len(candidates) == 1 else None
        return out

    pairs, used_a, used_b = [], set(), set()

    def take(qa, qb, method):
        if qa in used_a or qb in used_b or qa not in schema_a or qb not in schema_b:
            return
        used_a.add(qa)
        used_b.add(qb)
        pairs.append(
            {"a": qa, "b": qb, "key": schema_a[qa]["table"], "method": method}
        )

    for method, keyfunc in (("exact", lambda n: n), ("normalized", normalize)):
        index_a = index(schema_a, chosen_a, keyfunc)
        index_b = index(schema_b, chosen_b, keyfunc)
        for value, qa in index_a.items():
            qb = index_b.get(value)
            if qa and qb:
                take(qa, qb, method)

    for given in extra_pairs or []:
        take(given.get("a"), given.get("b"), given.get("method", "given"))

    return (
        pairs,
        sorted(q for q in schema_a if q not in used_a),
        sorted(q for q in schema_b if q not in used_b),
    )


def apply_matches(schema_a, schema_b, pairs):
    """Re-key both schemas so a matched pair shares one key, ready for `diff_schemas`.

    A matched pair is keyed by A's table name; anything unmatched keeps its qualified
    name, so it shows up as one-sided rather than pairing with something it is not.
    Every entry keeps its real `schema` and `table`, so later queries still hit the right
    object, plus `qualified` for display.
    """
    key_a = {p["a"]: p["key"] for p in pairs}
    key_b = {p["b"]: p["key"] for p in pairs}

    def rekey(schema, keys):
        out = {}
        for qualified, entry in schema.items():
            out[keys.get(qualified, qualified)] = {**entry, "qualified": qualified}
        return out

    return rekey(schema_a, key_a), rekey(schema_b, key_b)


def pair_by_name(schema_a, schema_b, chosen_a=None, chosen_b=None, extra_pairs=None):
    """Convenience wrapper: match the two schemas, then re-key them."""
    pairs, _, _ = match_tables(schema_a, schema_b, chosen_a, chosen_b, extra_pairs)
    return apply_matches(schema_a, schema_b, pairs)


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


def _names(items, limit):
    """`a, b, c (+97 more)` - a list the model can read without paying for all of it."""
    items = list(items)
    if not items:
        return "none"
    head = ", ".join(items[:limit])
    return head if len(items) <= limit else f"{head} (+{len(items) - limit} more)"


def row_key(table_diff):
    """The key a row comparison can actually use, or [] when there is none.

    A declared primary key is not enough: it has to exist on *both* sides. `Company` is
    keyed on `company_id` in B and has no key in A - and A does not even have that column -
    so it cannot be compared row by row, and saying otherwise costs the agent a step.
    Either side's key is accepted as long as all of its columns are present in both.
    """
    key = table_diff["primary_key_a"] or table_diff["primary_key_b"]
    if not key:
        return []
    one_sided = set(table_diff["columns_only_in_a"]) | set(table_diff["columns_only_in_b"])
    return [] if set(key) & one_sided else list(key)


def summarize_schema_diff(diff, label_a="A", label_b="B", max_tables=12, max_names=20,
                          row_compare_max=200000):
    """Compact text the LLM can read without burning the context window.

    This text is re-sent on every single LLM call of a run, so its size is multiplied by
    the number of steps: on two real databases the untruncated version was ~5,000 tokens
    a call, about 60% of a run's entire input. Detail is therefore spent where decisions
    are made - the tables that changed and the counts that disagree - and everything else
    is counted rather than listed. The UI and the report still get the full diff.
    """
    lines = []
    only_a, only_b = diff["tables_only_in_a"], diff["tables_only_in_b"]
    lines.append(f"Tables only in {label_a} ({len(only_a)}): {_names(only_a, max_names)}")
    lines.append(f"Tables only in {label_b} ({len(only_b)}): {_names(only_b, max_names)}")
    # Whether a table has a primary key and how big it is decides whether a row level
    # comparison is even possible, so it is stated up front. Without this the agent spends
    # a step per table discovering "no primary key" or "too large" the hard way.
    common = diff["common_tables"]
    # Whether a table has a usable key and how big it is decides whether a row level
    # comparison is possible at all, so it is stated up front: without it the agent spends
    # a step per table discovering "no primary key" or "too large" the hard way. Detail
    # goes to the tables that differ; the ones that already match are only counted.
    interesting = [
        t for t in common
        if not diff["table_diffs"][t]["identical"]
        or diff["table_diffs"][t]["row_count_a"] != diff["table_diffs"][t]["row_count_b"]
    ]
    settled = [t for t in common if t not in interesting]
    lines.append(
        f"Common tables ({len(common)}). These {len(interesting)} differ, as "
        "`name [key] rowsA/rowsB`. ANSWERED means the line is the whole answer - no tool "
        "call can add to it, so do not spend a step on one:"
    )
    for t in interesting[:max_names * 2]:
        d = diff["table_diffs"][t]
        key = row_key(d)
        label = ",".join(key) if key else "NO KEY - cannot compare rows"
        rows_a = "?" if d["row_count_a"] is None else f"{d['row_count_a']:,}"
        rows_b = "?" if d["row_count_b"] is None else f"{d['row_count_b']:,}"
        counts = [c for c in (d["row_count_a"], d["row_count_b"]) if c is not None]
        note = ""
        # Nothing further can be learned about an ANSWERED table by calling a tool: the
        # columns, types and counts here are the whole answer. Saying so is what stops
        # the agent spending a step per table finding out - and it is said once, in the
        # header, because a sentence repeated across 30 tables is itself a cost.
        if not key:
            note = " ANSWERED"
        elif counts and max(counts) > row_compare_max:
            note = " ANSWERED (too large; only a bounded aggregate can add anything)"
        lines.append(f"  {t} [{label}] {rows_a}/{rows_b}{note}")
    if len(interesting) > max_names * 2:
        lines.append(f"  (+{len(interesting) - max_names * 2} more differing tables)")
    if settled:
        lines.append(
            f"Identical in structure and row count ({len(settled)}): "
            f"{_names(settled, max_names)}"
        )

    changed = diff["tables_with_schema_changes"]
    for t in changed[:max_tables]:
        d = diff["table_diffs"][t]
        parts = []
        if d["columns_only_in_a"]:
            parts.append(
                f"columns only in {label_a}: {_names(d['columns_only_in_a'], 8)}"
            )
        if d["columns_only_in_b"]:
            parts.append(
                f"columns only in {label_b}: {_names(d['columns_only_in_b'], 8)}"
            )
        if d["type_changes"]:
            shown = [f"{tc['column']} {tc['a']} -> {tc['b']}" for tc in d["type_changes"][:5]]
            extra = len(d["type_changes"]) - len(shown)
            parts.append(
                "type changes: " + "; ".join(shown) + (f" (+{extra} more)" if extra else "")
            )
        if d["nullability_changes"]:
            parts.append(
                "nullability: "
                + _names([f"{n['column']} {n['a']}->{n['b']}" for n in d["nullability_changes"]], 5)
            )
        if d["default_changes"]:
            parts.append(f"{len(d['default_changes'])} default change(s)")
        if d["primary_key_changed"]:
            parts.append(f"primary key {d['primary_key_a']} -> {d['primary_key_b']}")
        if d["indexes_only_in_a"]:
            parts.append(f"indexes only in {label_a}: {_names(d['indexes_only_in_a'], 5)}")
        if d["indexes_only_in_b"]:
            parts.append(f"indexes only in {label_b}: {_names(d['indexes_only_in_b'], 5)}")
        lines.append(f"- {t}: " + "; ".join(parts))

    if len(changed) > max_tables:
        rest = changed[max_tables:]
        lines.append(
            f"- {len(rest)} further table(s) also changed: {_names(rest, max_names)}"
            " (ask compare_schema or compare_table_data for the detail)"
        )

    mismatches = diff["row_count_mismatches"]
    for rc in mismatches[:max_tables * 2]:
        lines.append(f"- {rc['table']}: row count {rc['a']} vs {rc['b']}")
    if len(mismatches) > max_tables * 2:
        lines.append(f"- and {len(mismatches) - max_tables * 2} more row count mismatch(es)")
    return "\n".join(lines)


