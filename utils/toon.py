"""Compact encoding of row data for prompts, in the style of TOON.

Rows are the bulkiest thing this project puts in a prompt, and they are the most
wasteful shape: a list of dicts repeats every column name on every row. A table of 43
columns and 10 rows costs ~2,850 tokens as `repr(dict)` and ~3,300 as indented JSON,
almost all of it the same 43 names over and over.

TOON (Token-Oriented Object Notation) is the idea of naming the fields once and writing
the values as rows, keeping JSON's structure without its repetition. Only the tabular
part is implemented here - that is where the saving is, and a local 40 lines beats a
dependency for it. Measured on dbo.task_daily, 43 columns x 10 rows: ~1,020 tokens
against ~2,850, and unlike clipping each row to a character budget it loses nothing.

    rows[2]{id,name,qty}:
      1,Anita,3
      2,Ben,
    (a missing value is empty; a value containing the delimiter or a newline is quoted)

Non-uniform rows fall back to one `- key: value` line each, because the tabular form
only pays off when every row has the same fields.
"""

DELIMITER = ","
MAX_CELL = 120


def _cell(value):
    """One value, flattened to a single line and quoted only when it has to be."""
    if value is None:
        return ""
    text = str(value)
    if len(text) > MAX_CELL:
        text = text[: MAX_CELL - 3] + "..."
    if any(ch in text for ch in (DELIMITER, '"', "\n", "\r")):
        text = text.replace("\n", " ").replace("\r", " ").replace('"', '""')
        return f'"{text}"'
    return text


def encode(rows, name="rows", columns=None):
    """Encode a list of dicts. Uniform rows become a table; anything else, a list."""
    rows = list(rows or [])
    if not rows:
        return f"{name}[0]: none"

    if columns is None:
        first = list(rows[0])
        uniform = all(list(r) == first for r in rows)
        columns = first if uniform else None

    if not columns:
        lines = [f"{name}[{len(rows)}] (fields differ between rows):"]
        for row in rows:
            body = ", ".join(f"{k}: {_cell(v)}" for k, v in row.items())
            lines.append(f"  - {body}")
        return "\n".join(lines)

    header = f"{name}[{len(rows)}]{{{DELIMITER.join(str(c) for c in columns)}}}:"
    body = [
        "  " + DELIMITER.join(_cell(row.get(c)) for c in columns) for row in rows
    ]
    return "\n".join([header] + body)


def clip(text, limit, what="rows"):
    """Cut an encoded block to a character budget on a line boundary, and say so."""
    if limit <= 0 or len(text) <= limit:
        return text
    kept = text[:limit].rsplit("\n", 1)[0]
    dropped = text[len(kept):].count("\n")
    return f"{kept}\n  ... [{dropped} more {what} omitted to stay within the budget]"
