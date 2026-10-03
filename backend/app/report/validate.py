"""The self-check of a report: does it account for everything, and do the files agree with it?

A report is only marked done when this passes; otherwise the problems are shown with it."""
from docx import Document
from openpyxl import load_workbook

from . import plain
from . import xlsx_writer as X


def evidence(ev, expected_part1=None, expected_part2=None):
    """Problems in the evidence itself (empty list: none)."""
    problems = []
    p1, p2 = ev["part1"]["items"], ev["part2"]["items"]
    if expected_part1 is not None and len(p1) != expected_part1:
        problems.append(f"Section 1 has {len(p1)} tables, the migration plan requires {expected_part1}.")
    if expected_part2 is not None and len(p2) != expected_part2:
        problems.append(f"Section 2 has {len(p2)} mappings, the migration plan has {expected_part2}.")
    for label, items in (("Section 1", p1), ("Section 2", p2)):
        ids = [i["id"] for i in items]
        dup = sorted({x for x in ids if ids.count(x) > 1})
        if dup:
            problems.append(f"{label} lists {', '.join(dup[:5])} more than once.")
        bad = [i["id"] for i in items if i["result"] not in plain.RESULTS]
        if bad:
            problems.append(f"{label} has items without a valid result: {', '.join(bad[:5])}.")
    known = {i["id"] for i in p1 + p2}
    orphan = [i["id"] for i in ev["issues"] if i["item"] not in known]
    if orphan:
        problems.append(f"{len(orphan)} issues point at no table or mapping ({', '.join(orphan[:5])}).")
    # Every item graded Must fix or Needs a decision has at least one issue that says why.
    with_issue = {i["item"] for i in ev["issues"]}
    silent = [i["id"] for i in p1 + p2 if i["result"] in (plain.MUST_FIX, plain.DECIDE) and i["id"] not in with_issue
              and not i.get("reason")]
    if silent:
        problems.append(f"{len(silent)} items are graded without a reason ({', '.join(silent[:5])}).")
    if ev["overall"] != plain.overall(i["result"] for i in p1 + p2):
        problems.append("The overall result does not follow from the items.")
    return problems


def files(ev, xlsx_path, docx_path):
    """Problems in the written files: reopened and compared with the evidence."""
    problems = []
    try:
        wb = load_workbook(xlsx_path)
        expected = {X.SUMMARY: None, X.P1_TABLES: len(ev["part1"]["items"]),
                    X.P2_MAPPINGS: len(ev["part2"]["items"])}
        for name, rows in expected.items():
            if name not in wb.sheetnames:
                problems.append(f"The Excel file has no sheet {name!r}.")
            elif rows is not None:
                ws = wb[name]
                found = sum(1 for r in ws.iter_rows(min_row=X.FIRST, max_col=1, values_only=True) if r[0] not in (None, ""))
                if found != rows:
                    problems.append(f"Excel sheet {name!r} has {found} rows, the evidence {rows}.")
        # Every issue is written on the row of its table (Section 1) or mapping (Section 2).
        for part, name, col in ((1, X.P1_TABLES, X.P1_ISSUES_COL), (2, X.P2_MAPPINGS, X.P2_ISSUES_COL)):
            if name in wb.sheetnames:
                text = "\n".join(str(r[0] or "") for r in wb[name].iter_rows(
                    min_row=X.FIRST, min_col=col, max_col=col, values_only=True))
                lost = [i["id"] for i in ev["issues"] if i["part"] == part and f"{i['id']} " not in text]
                if lost:
                    problems.append(f"Excel sheet {name!r} does not show issues {', '.join(lost[:5])}.")
    except Exception as exc:
        problems.append(f"The Excel file cannot be opened again: {exc}")
    try:
        doc = Document(docx_path)
        text = "\n".join(p.text for p in doc.paragraphs)
        cells = "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
        if f"Overall result: {ev['overall']}" not in cells:
            problems.append("The Word file does not show the overall result.")
        if "Summary" not in text:
            problems.append("The Word file has no summary.")
        appendix = sum(1 for t in doc.tables for row in t.rows[1:] if row.cells and row.cells[-1].text in plain.RESULTS)
        if appendix < len(ev["part1"]["items"]) + len(ev["part2"]["items"]):
            problems.append("The Word report does not list every table and mapping.")
    except Exception as exc:
        problems.append(f"The Word file cannot be opened again: {exc}")
    return problems
