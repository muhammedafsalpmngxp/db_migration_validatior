"""The Excel workbook of a report, built only from its evidence.

    Summary                     both sections in one page: result, counts, databases, links
    1. ATNM → RDS               one row per required table: source table and its records,
                                target table and its records, then every issue found
    2. RDS → New System         one row per mapping: the same, with the expected records
    Tables Not In Plan          tables in the RDS databases that no mapping uses (information)
    Words Used                  what the words mean

On the two main sheets each row says what was found in plain words: one numbered line per
issue ("1-001 Must fix: ..."), "Not checked: ..." for a part that could not be checked,
"Note: ..." for values changed on purpose, and "No issue: ..." with what was proven. Rows are
coloured by result, problems first; every list has a header (row 3), filters, a frozen header
and prints landscape on one page wide. The Summary counts are formulas over the main sheets.
"""
from datetime import datetime

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.properties import PageSetupProperties

from . import plain

FONT = "Arial"
THIN = Side(style="thin", color="A6A6A6")
GRID = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TONE = {plain.CORRECT: ("C6EFCE", "006100"), plain.MUST_FIX: ("FFC7CE", "9C0006"), plain.DECIDE: ("FFEB9C", "9C5700"),
        plain.NOT_CHECKED: ("D9D9D9", "3F3F3F"), plain.BY_DESIGN: ("DDEBF7", "1F4E78"),
        "NOT READY": ("FFC7CE", "9C0006"), "INCOMPLETE": ("FFEB9C", "9C5700"), "READY": ("C6EFCE", "006100")}
# Light colour of a whole row, by its result.
ROW_TONE = {plain.CORRECT: "EBF7EE", plain.MUST_FIX: "FDECEE", plain.DECIDE: "FFF7DB", plain.NOT_CHECKED: "F2F2F2",
            plain.BY_DESIGN: "EEF4FB"}
NAVY = "1F3864"
SECTION1 = {"tab": "2E75B6", "head": "1F4E79", "name": "Section 1 · ATNM → RDS"}
SECTION2 = {"tab": "548235", "head": "385723", "name": "Section 2 · RDS → new system"}
OTHER = {"tab": "A6A6A6", "head": "404040", "name": "Other"}
FIRST = 4           # first data row of every list sheet (1 title, 2 what it shows, 3 header)
DIFF = "#,##0;[Red]-#,##0;0"
ONE_TO_ONE = "target rows = source rows"

# The sheets the Summary and the self-check (validate.py) read.
SUMMARY = "Summary"
P1_TABLES = "1. ATNM → RDS"
P2_MAPPINGS = "2. RDS → New System"
# Result and Issues found: their columns on the two main sheets
P1_RESULT_COL, P1_ISSUES_COL = "I", 10
P2_RESULT_COL, P2_ISSUES_COL = "J", 11
NOT_IN_PLAN, WORDS = "Tables Not In Plan", "Words Used"


def local(ts):
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _by_result(rows, result_of):
    """Most severe first, the input order kept within a result."""
    return sorted(rows, key=lambda x: plain.ORDER.get(result_of(x), len(plain.ORDER)))


def _sheet(wb, name, section, intro, columns, rows, result_cols=(), wrap=(), number_formats=None, title=None,
           row_result=None):
    """One list sheet: title, what it shows, header, rows, filter, frozen header, widths, result
    colours, landscape print one page wide. `row_result` (column number): colour each row by it."""
    ws = wb.create_sheet(name)
    ws.sheet_properties.tabColor = section["tab"]
    ws.cell(row=1, column=1, value=title or f"{section['name']} · {name.split('. ', 1)[-1]}").font = Font(
        name=FONT, size=13, bold=True, color=section["head"])
    ws.cell(row=2, column=1, value=intro + ("" if rows else "  Nothing to list.")).font = Font(
        name=FONT, size=9, italic=True, color="595959")
    head = PatternFill("solid", fgColor=section["head"])
    for c, (text, width) in enumerate(columns, 1):
        cell = ws.cell(row=FIRST - 1, column=c, value=text)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
        cell.fill = head
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = GRID
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[FIRST - 1].height = 30
    for r, values in enumerate(rows, FIRST):
        fill = None
        if row_result:
            fill = PatternFill("solid", fgColor=ROW_TONE.get(values[row_result - 1], "FFFFFF"))
        for c, v in enumerate(values, 1):
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x) for x in v)
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = Font(name=FONT, size=10)
            cell.border = GRID
            cell.alignment = Alignment(vertical="top", wrap_text=c in wrap)
            if fill:
                cell.fill = fill
            if isinstance(v, int) and not isinstance(v, bool):
                cell.number_format = (number_formats or {}).get(c, "#,##0")
            elif isinstance(v, float):
                cell.number_format = (number_formats or {}).get(c, "0.00%")
    last = FIRST - 1 + max(1, len(rows))
    ws.freeze_panes = f"A{FIRST}"
    ws.auto_filter.ref = f"A{FIRST - 1}:{get_column_letter(len(columns))}{last}"
    for rc in result_cols:
        col = get_column_letter(rc)
        for word, (fill, color) in TONE.items():
            ws.conditional_formatting.add(f"{col}{FIRST}:{col}{last}", FormulaRule(
                formula=[f'{col}{FIRST}="{word}"'], fill=PatternFill("solid", fgColor=fill),
                font=Font(name=FONT, color=color, bold=True)))
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = f"{FIRST - 1}:{FIRST - 1}"
    return ws


# ---- what goes in a row of the main sheets ---------------------------------------------------------------

def _lines(item, issues, not_checked, notes, ok_text):
    """(Issues found, What to do) of one table or mapping, one numbered line each."""
    found, todo = [], []
    for i in issues:
        col = f" (column: {i['column']})" if i["column"] and i["column"] not in i["finding"] else ""
        found.append(f"{i['id']} {i['result']}: {i['finding']}{col}")
        if i["action"] not in todo:
            todo.append(i["action"])
    for n in not_checked:
        found.append(f"Not checked: {n['what']} - {n['reason']}")
        if n["todo"] not in todo:
            todo.append(n["todo"])
    found += [f"Note: {x}" for x in notes]
    if not issues and not not_checked:
        found.insert(0, f"No issue: {ok_text}" if ok_text else "No issue.")
    if item["result"] == plain.BY_DESIGN and not issues and not not_checked:
        found[0] = f"By design: {ok_text}"
    return ("\n".join(found),
            "\n".join(f"{n}. {x}" for n, x in enumerate(todo, 1)) if len(todo) > 1 else (todo[0] if todo else "—"))


def _columns_text1(i):
    """The columns of a Section 1 table in a few words."""
    if i["columns_source"] is None or i["columns_target"] is None:
        return "—"
    parts = [f"{n} {what}" for n, what in ((i["columns_missing"], "missing in RDS"), (i["columns_extra"], "only in RDS"),
                                            (i["columns_changed"], "format changed"), (i["columns_renamed"], "renamed"))
             if n]
    return f"{i['columns_source']} columns: " + (", ".join(parts) if parts else "all the same")


COL_WORDS = [("Same", "same"), ("Renamed (name rules)", "renamed by name"), ("Renamed (proven by data)", "renamed (proven)"),
             ("Changed", "changed (format, key or empty-allowed; see the column map)"), ("Not migrated", "not moved"), ("Only in target", "new in target")]


def _columns_text2(cols):
    """The columns of a mapping in a few words."""
    if not cols:
        return "—"
    count = {}
    for c in cols:
        count[c["result"]] = count.get(c["result"], 0) + 1
    parts = [f"{count[k]} {w}" for k, w in COL_WORDS if count.get(k)]
    parts += [f"{v} {k.lower()}" for k, v in count.items() if k not in dict(COL_WORDS)]
    n = sum(1 for c in cols if c["column"])
    return f"{n} source columns: " + ", ".join(parts)


def _renamed(renames, rows_key):
    out = []
    for r in renames:
        if r.get("verdict") == "verified":
            n = r.get(rows_key)
            out.append(f"{r['source']} → {r['target']}" + (f" (identical on {n:,} of {n:,} records)"
                                                         if isinstance(n, int) else ""))
    return "\n".join(out) or "—"


def _tables_cell(tables, fallback_name, fallback_rows):
    """(names, records) of a mapping's tables: one line each; a number when there is one table."""
    if not tables:
        return fallback_name, fallback_rows
    names = "\n".join(t["name"] + ("" if t.get("exists", True) else " (does not exist)") for t in tables)
    if len(tables) == 1:
        return names, tables[0]["rows"]
    return names, "\n".join("—" if t["rows"] is None else f"{t['rows']:,}" for t in tables)


# ---- the sheets ----------------------------------------------------------------------------------------

def _empty_text(cols, before, after):
    """Empty values (NULL) of a table's columns in a few words: `cols` is
    [(column, empty before, empty after, empty on every record of both sides)]. A column that is
    empty everywhere differs only when the record counts do, so it is not listed as a difference."""
    cols = [c for c in cols if c[1] is not None or c[2] is not None]
    if not cols:
        return "—"
    diff = [c for c in cols if c[1] != c[2] and not c[3]]
    if not diff:
        total = sum(c[1] or 0 for c in cols)
        return (f"no difference in {len(cols)} columns ({total:,} empty in {before})" if total
                else f"none in all {len(cols)} columns")
    lines = [f"{c[0]}: {_n(c[1])} empty in {before}, {_n(c[2])} in {after}" for c in diff[:6]]
    if len(diff) > 6:
        lines.append(f"…and {len(diff) - 6} more columns")
    return "\n".join(lines)


def _n(x):
    return "?" if x is None else f"{x:,}"


def _main1(wb, ev):
    p1 = ev["part1"]
    rows = []
    for i in _by_result(p1["items"], lambda x: x["result"]):
        issues = [x for x in ev["issues"] if x["item"] == i["id"]]
        nc = [n for n in ev["not_checked"] if n["part"] == 1 and n["item"] == i["table"]
              and n["database"] == i["source_db"]]
        mine = lambda xs: [x for x in xs if x.get("table") == i["table"] and x.get("database") == i["source_db"]]
        notes = []
        for d in mine(p1["row_diffs"]):
            for key, what in (("missing_keys", "missing in RDS"), ("extra_keys", "extra in RDS"),
                              ("changed_keys", "changed")):
                if d.get(key):
                    notes.append(f"records {what}, e.g. keys {d[key]}")
        empty = _empty_text([(e["column"], e["null_source"], e["null_target"],
                              e["null_source"] == e["rows_source"] and e["null_target"] == e["rows_target"])
                             for e in mine(p1["empty"])], "ATNM", "RDS")
        found, todo = _lines(i, issues, nc, notes, i.get("values_detail") or i["reason"])
        rows.append([
            f"{i['source_db']} · {i['table']}" if i["in_source"] is not False else f"Not in ATNM ({i['table']})",
            i["rows_source"],
            f"{i['target_db']} · {i['table']}" if i["in_target"] is not False else "Not in RDS",
            i["rows_target"], i["rows_diff"], _columns_text1(i), _renamed(mine(p1["renames"]), "paired"), empty,
            i["result"], found, todo, local(i["checked_at"])])
    _sheet(wb, P1_TABLES, SECTION1, "Every required table (the tables the migration uses), copied from the ATNM server "
                                    "to RDS. The copy must be exact. Problems first; the issue numbers match the Word "
                                    "report.",
           [("Source table (ATNM)", 34), ("Row count", 11), ("Target table (RDS)", 36), ("Row count", 11),
            ("Difference", 10), ("Columns", 26), ("Renamed columns", 28), ("Empty values (NULL)", 30),
            ("Result", 15), ("Issues found", 70), ("What to do", 42), ("Checked at", 16)],
           rows, result_cols=(9,), wrap=(1, 3, 6, 7, 8, 10, 11), number_formats={5: DIFF},
           title="Section 1 · ATNM → RDS: copy of the client's databases", row_result=9)


def _main2(wb, ev):
    p2 = ev["part2"]
    rows = []
    for i in _by_result(p2["items"], lambda x: x["result"]):
        issues = [x for x in ev["issues"] if x["item"] == i["id"]]
        nc = [n for n in ev["not_checked"] if n["part"] == 2 and n["item"] == i["mapping"]]
        flagged = {c.strip() for x in issues for c in (x["column"] or "").replace("→", ",").split(",")}
        values = [v for v in p2["values"] if v["mapping"] == i["mapping"]]
        notes = []
        for v in values:
            if v["column"].split(" →")[0].strip() in flagged:
                continue
            for key, text in (("recoded", "values turned into ids through the lookup list"),
                              ("case_only", "values differ only in capital/small letters"),
                              ("blank_to_null", "blank texts became empty")):
                if v.get(key):
                    notes.append(f"{v['column']}: {v[key]:,} {text}.")
        renames = [r for r in p2["renames"] if r["mapping"] == i["mapping"]]
        cols = [c for c in p2["columns"] if c["mapping"] == i["mapping"]]
        src, src_rows = _tables_cell(i.get("source_tables"), f"{i['source_db']} · {i['source']}", i["rows_expected"])
        tgt, tgt_rows = _tables_cell(i.get("target_tables"), f"{i['target_db']} · {i['target']}", i["rows_actual"])
        expected = i["rows_expected"]
        if i["rule"] and i["rule"] != ONE_TO_ONE and expected is not None:
            expected = f"{expected:,} ({i['rule']})"
        elif expected is None and i["rule"]:
            expected = i["rule"]
        empty = _empty_text([(v["column"], v.get("null_source"), v.get("null_target"), False) for v in values],
                            "source", "target")
        found, todo = _lines(i, issues, nc, notes, i.get("values_detail") or i["reason"])
        rows.append([i["mapping"], src, src_rows, tgt, tgt_rows, expected, _columns_text2(cols),
                     _renamed(renames, "rows_checked"), empty, i["result"], found, todo, local(i["checked_at"])])
    tdb = next((d["name"] for d in p2.get("databases") or [] if d["role"] == "target"), "the new system")
    _sheet(wb, P2_MAPPINGS, SECTION2, f"Every mapping of the migration plan: source table(s) on RDS → target table(s) in "
                                      f"{tdb}. Problems first; the issue numbers match the Word report.",
           [("Mapping", 24), ("Source table(s) (RDS)", 34), ("Row count", 11), (f"Target table(s) ({tdb})", 34),
            ("Row count", 11), ("Expected rows (rule)", 18), ("Columns", 28), ("Renamed columns", 28),
            ("Empty values (NULL)", 30), ("Result", 15), ("Issues found", 70), ("What to do", 42), ("Checked at", 16)],
           rows, result_cols=(10,), wrap=(2, 3, 4, 5, 6, 7, 8, 9, 11, 12),
           title=f"Section 2 · RDS → {tdb}: move into the new system", row_result=10)


def _others(wb, ev):
    """The two sheets kept besides the main ones: tables no mapping uses, and the words."""
    cov = []
    for db in (ev.get("coverage") or {}).get("databases") or []:
        for t in db.get("not_in_plan") or []:
            cov.append([db["name"], f"{t['schema']}.{t['table']}", t["rows"]])
    _sheet(wb, NOT_IN_PLAN, OTHER, "For information: tables in the RDS databases that the migration plan does not use. "
                                   "Confirm none was forgotten.",
           [("Database", 24), ("Table", 44), ("Records", 14)], cov, title="Tables not in the migration plan")
    _sheet(wb, WORDS, OTHER, "What the words in this report mean.", [("Word", 28), ("Meaning", 110)],
           [list(w) for w in plain.GLOSSARY] + [[k, v] for k, v in plain.RESULT_HELP.items()], wrap=(2,),
           title="Words used")


# ---- the Summary ----------------------------------------------------------------------------------------

def _put(ws, r, c, value, size=10, bold=False, italic=False, color=None, fill=None, wrap=False):
    cell = ws.cell(row=r, column=c, value=value)
    cell.font = Font(name=FONT, size=size, bold=bold, italic=italic, color=color)
    if fill:
        cell.fill = PatternFill("solid", fgColor=fill)
    cell.alignment = Alignment(vertical="top", wrap_text=wrap)
    return cell


def _link(ws, r, c, sheet, text=None):
    cell = _put(ws, r, c, text or sheet)
    cell.hyperlink = Hyperlink(ref=cell.coordinate, location=f"'{sheet}'!A1")
    cell.font = Font(name=FONT, color="0563C1", underline="single")


def _text_row(ws, r, label, text):
    """A label and a text over columns B-G, the row as high as the text needs."""
    _put(ws, r, 1, label, bold=True)
    _put(ws, r, 2, text, wrap=True)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=7)
    lines = sum(max(1, -(-len(part) // 95)) for part in str(text or "").splitlines() or [""])
    if lines > 1:
        ws.row_dimensions[r].height = 14 * lines + 4


def _summary(wb, ev):
    ws = wb[SUMMARY]
    ws.sheet_properties.tabColor = NAVY
    run, p1, p2, ai = ev["run"], ev["part1"], ev["part2"], ev.get("summary") or {}
    tdb = next((d["name"] for d in p2.get("databases") or [] if d["role"] == "target"), "the new system")
    ws.column_dimensions["A"].width = 30
    for c in "BCDEFG":
        ws.column_dimensions[c].width = 15
    _put(ws, 1, 1, "Database Migration Check", 18, True, color=NAVY)
    if (run.get("test") or {}).get("text"):
        _put(ws, 2, 1, run["test"]["text"], 12, True, color="9C0006")
    _put(ws, 3, 1, f"ATNM → RDS → {tdb} · checked {local(run.get('started_at'))} → {local(run.get('finished_at'))}",
         italic=True, color="595959")

    r = 5
    _put(ws, r, 1, "Overall result", 13, True)
    t1, t2 = f"'{P1_TABLES}'!{P1_RESULT_COL}:{P1_RESULT_COL}", f"'{P2_MAPPINGS}'!{P2_RESULT_COL}:{P2_RESULT_COL}"
    must, nc = plain.MUST_FIX, plain.NOT_CHECKED
    _put(ws, r, 2, f"=IF(COUNTIF({t1},\"{must}\")+COUNTIF({t2},\"{must}\")>0,\"NOT READY\","
                   f"IF(COUNTIF({t1},\"{nc}\")+COUNTIF({t2},\"{nc}\")>0,\"INCOMPLETE\",\"READY\"))", 13, True)
    for word, (fill, color) in TONE.items():
        ws.conditional_formatting.add(f"B{r}", FormulaRule(formula=[f'B{r}="{word}"'], fill=PatternFill("solid", fgColor=fill),
                                                           font=Font(name=FONT, size=13, bold=True, color=color)))
    _put(ws, r, 3, plain.OVERALL_HELP.get(ev["overall"], ""), italic=True, color="595959")
    if "stopped early" in (run.get("mode") or ""):
        r += 1
        _put(ws, r, 1, "The run was stopped early: this is a partial report.", bold=True, color="9C0006")
    r += 2
    if ai:
        _text_row(ws, r, "In short", ai.get("executive_summary", ""))
        r += 1
        _put(ws, r, 2, "written by AI from the facts and checked" if ai.get("source") == "ai"
             else "plain sentences from the facts", 8, italic=True, color="7F7F7F")
        r += 2

    def section(r, sec, title, databases, sheet, col, word, part, summary, by_design):
        _put(ws, r, 1, title, 12, True, color="FFFFFF", fill=sec["head"])
        for c in range(2, 8):
            ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=sec["head"])
        r += 1
        for n, line in enumerate(databases):
            _text_row(ws, r, "Databases" if n == 0 else "", line)
            r += 1
        heads = [plain.CORRECT, plain.MUST_FIX, plain.DECIDE, plain.NOT_CHECKED] + ([plain.BY_DESIGN] if by_design else [])
        _put(ws, r, 1, f"{word} checked", bold=True)
        for c, h in enumerate(heads + ["Total"], 2):
            cell = _put(ws, r, c, h, 9, True, color=TONE.get(h, ("", "000000"))[1], fill=TONE.get(h, ("F2F2F2",))[0])
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
            cell.border = GRID
        r += 1
        _link(ws, r, 1, sheet, f"{word} (open the sheet)")
        for c, h in enumerate(heads, 2):
            _put(ws, r, c, f"=COUNTIF('{sheet}'!{col}:{col},\"{h}\")", 11, True).alignment = Alignment(horizontal="center")
        _put(ws, r, 2 + len(heads), f"=SUM(B{r}:{get_column_letter(1 + len(heads))}{r})", 11, True).alignment = \
            Alignment(horizontal="center")
        for c in range(2, 3 + len(heads)):
            ws.cell(row=r, column=c).border = GRID
        r += 1
        _put(ws, r, 1, "Issues found", bold=True)
        for c, h in enumerate(heads[:4], 2):
            if h in (plain.MUST_FIX, plain.DECIDE):
                n = sum(1 for i in ev["issues"] if i["part"] == part and i["result"] == h)
                _put(ws, r, c, n, 11, True).alignment = Alignment(horizontal="center")
        r += 1
        if summary:
            _text_row(ws, r, "Summary", summary)
            r += 1
        return r + 1

    dbs1 = []
    for d in p1.get("databases") or []:
        line = f"{d['source_db']} (ATNM) → {d['target_db']} (RDS): {d['required']} tables checked"
        if d.get("tables_source") is not None and d.get("not_required") is not None:
            line += (f" - the ATNM database has {d['tables_source']} tables, {d['tables_source'] - d['not_required']} "
                     f"of them used by the migration")
        dbs1.append(line)
    r = section(r, SECTION1, "SECTION 1 · ATNM → RDS: copy of the client's databases (must be exact)", dbs1 or ["—"],
                P1_TABLES, P1_RESULT_COL, "Tables", 1, ai.get("part1_summary"), False)
    srcs = ", ".join(d["name"] for d in p2.get("databases") or [] if d["role"] != "target")
    r = section(r, SECTION2, f"SECTION 2 · RDS → {tdb}: move into the new system", [f"{srcs} (RDS) → {tdb}"],
                P2_MAPPINGS, P2_RESULT_COL, "Mappings", 2, ai.get("part2_summary"), True)

    cov = (ev.get("coverage") or {}).get("databases") or []
    if cov:
        _put(ws, r, 1, "Tables not in the plan", 12, True, color=NAVY)
        r += 1
        for db in cov:
            _text_row(ws, r, db["name"], f"{len(db.get('not_in_plan') or [])} of {db.get('tables', '?')} tables are not "
                                         "used by the migration plan (listed for information).")
            r += 1
        _link(ws, r, 1, NOT_IN_PLAN, "See the list")
        r += 2

    _put(ws, r, 1, "What the results mean", 12, True, color=NAVY)
    r += 1
    for word in plain.RESULTS:
        fill, color = TONE[word]
        _text_row(ws, r, word, plain.RESULT_HELP[word])
        _put(ws, r, 1, word, bold=True, color=color, fill=fill)
        r += 1
    r += 1
    _put(ws, r, 1, "Other sheets", 12, True, color=NAVY)
    details = [n for n in wb.sheetnames if n not in (SUMMARY, P1_TABLES, P2_MAPPINGS)]
    for n, name in enumerate(details):
        _link(ws, r + 1 + n // 3, 1 + (n % 3) * 2, name)
    r += 2 + (len(details) + 2) // 3
    _put(ws, r, 1, f"Run {run.get('id')}{' · ' + run['mode'] if run.get('mode') else ''}. Read-only check: nothing was "
                   "changed in any database. No passwords or server addresses are in this file.", 8, italic=True,
         color="7F7F7F")


def build(ev, path):
    wb = Workbook()
    wb.active.title = SUMMARY
    _main1(wb, ev)
    _main2(wb, ev)
    _others(wb, ev)
    _summary(wb, ev)
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    wb.save(path)
