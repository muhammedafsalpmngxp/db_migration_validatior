"""The Excel workbook of a report, built only from its evidence.

    Summary                     the whole report in one short paragraph, in plain words
    1. ATNM → RDS               one row per required table: source table and its records,
                                target table and its records, then every issue found
    2. RDS → New System         one row per mapping: the same, with the expected records
    Tables Not In Plan          tables in the RDS databases that no mapping uses (information)
    Words Used                  what the words mean
    Full Details                the whole report in words: about, result, table count, results
                                by step, the findings of each section, decisions, how it was
                                checked, notes, what the status means (no per-table list)

On the two main sheets each row says what was found in plain words: one numbered line per
issue ("1-001 Need to fix: ..."), "Not checked: ..." for a part that could not be checked,
"Note: ..." for values changed on purpose, and "No issue: ..." with what was proven. Rows are
coloured by status, problems first; every list has a header (row 3), filters, a frozen header
and prints landscape on one page wide. The workbook writes Must fix as "Need to fix"
(plain.shown); the Full Details counts are formulas over the main sheets.
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
# Colours by the word the workbook shows (plain.shown: "Need to fix" for Must fix).
S = plain.shown
TONE = {S(plain.CORRECT): ("C6EFCE", "006100"), S(plain.MUST_FIX): ("FFC7CE", "9C0006"),
        S(plain.DECIDE): ("FFEB9C", "9C5700"), S(plain.NOT_CHECKED): ("D9D9D9", "3F3F3F"),
        S(plain.BY_DESIGN): ("DDEBF7", "1F4E78"),
        "NOT READY": ("FFC7CE", "9C0006"), "INCOMPLETE": ("FFEB9C", "9C5700"), "READY": ("C6EFCE", "006100")}
# Light colour of a whole row, by its result.
ROW_TONE = {S(plain.CORRECT): "EBF7EE", S(plain.MUST_FIX): "FDECEE", S(plain.DECIDE): "FFF7DB",
            S(plain.NOT_CHECKED): "F2F2F2", S(plain.BY_DESIGN): "EEF4FB"}
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
NOT_IN_PLAN, WORDS, FULL = "Tables Not In Plan", "Words Used", "Full Details"


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
        found.append(f"{i['id']} {S(i['result'])}: {i['finding']}{col}")
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
            f"{i['target_db']} · {i.get('target_table') or i['table']}" if i["in_target"] is not False else "Not in RDS",
            i["rows_target"], i["rows_diff"], _columns_text1(i), _renamed(mine(p1["renames"]), "paired"), empty,
            S(i["result"]), found, todo, local(i["checked_at"])])
    _sheet(wb, P1_TABLES, SECTION1, "Every required table (the tables the migration uses), copied from the ATNM server "
                                    "to RDS. The copy must be exact. Problems first; the issue numbers match the Word "
                                    "report.",
           [("Source table (ATNM)", 34), ("Row count", 11), ("Target table (RDS)", 36), ("Row count", 11),
            ("Difference", 10), ("Columns", 26), ("Renamed columns", 28), ("Null values", 30),
            ("Status", 15), ("Issues found", 70), ("What to do", 42), ("Checked at", 16)],
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
        notes = list(i.get("notes") or [])
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
                     _renamed(renames, "rows_checked"), empty, S(i["result"]), found, todo, local(i["checked_at"])])
    tdb = next((d["name"] for d in p2.get("databases") or [] if d["role"] == "target"), "the new system")
    _sheet(wb, P2_MAPPINGS, SECTION2, f"Every mapping of the migration plan: source table(s) on RDS → target table(s) in "
                                      f"{tdb}. Problems first; the issue numbers match the Word report.",
           [("Mapping", 24), ("Source table(s) (RDS)", 34), ("Row count", 11), (f"Target table(s) ({tdb})", 34),
            ("Row count", 11), ("Expected rows (rule)", 18), ("Columns", 28), ("Renamed columns", 28),
            ("Null values", 30), ("Status", 15), ("Issues found", 70), ("What to do", 42), ("Checked at", 16)],
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
           [list(w) for w in plain.GLOSSARY] + [[S(k), v] for k, v in plain.RESULT_HELP.items()], wrap=(2,),
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


def _and(words, last="and"):
    words = [w for w in words if w]
    return ", ".join(words[:-1]) + f" {last} " + words[-1] if len(words) > 1 else (words[0] if words else "")


def _count(items, *results):
    return sum(1 for i in items if i["result"] in results)


def _name(item):
    """A table (Section 1) or mapping (Section 2) by its short name."""
    return item["table"].split(".")[-1] if item["part"] == 1 else item["mapping"]


def _kinds(ev, part, result):
    """[(kind, issues, [names])] of one section's issues with one result: the kinds that touch the most
    tables first; the names with the biggest record difference first."""
    items = {i["id"]: i for i in ev[f"part{part}"]["items"]}
    groups = {}
    for x in ev["issues"]:
        if x["part"] == part and x["result"] == result and x["item"] in items:
            g = groups.setdefault(x["check"], {"issues": 0, "items": {}})
            g["issues"] += 1
            g["items"][x["item"]] = items[x["item"]]
    out = []
    for kind, g in groups.items():
        its = sorted(g["items"].values(), key=lambda i: -abs(i.get("rows_diff") or 0))
        out.append((kind, g["issues"], [_name(i) for i in its]))
    return sorted(out, key=lambda k: (-len(k[2]), -k[1]))


def _target_db(ev):
    return next((d["name"] for d in ev["part2"].get("databases") or [] if d["role"] == "target"), "the new system")


def _checked_line(ev):
    run = ev["run"]
    return (f"Checked {local(run.get('started_at'))} → {local(run.get('finished_at'))}  ·  client server (ATNM) → RDS "
            f"→ new system ({_target_db(ev)})")


def _title(ws, ev, text):
    """Rows 1-3: the title, the test mode line (row 2, when on) and when it was checked."""
    run = ev["run"]
    _put(ws, 1, 1, text, 18, True, color=NAVY)
    if (run.get("test") or {}).get("text"):
        _put(ws, 2, 1, run["test"]["text"], 12, True, color="9C0006")
    _put(ws, 3, 1, _checked_line(ev), italic=True, color="595959")


def _summary_text(ev):
    """The whole report in one paragraph, in plain words, from the evidence."""
    i1, i2 = ev["part1"]["items"], ev["part2"]["items"]
    m1, d1, n1 = _count(i1, plain.MUST_FIX), _count(i1, plain.DECIDE), _count(i1, plain.NOT_CHECKED)
    m2, d2, n2 = _count(i2, plain.MUST_FIX), _count(i2, plain.DECIDE), _count(i2, plain.NOT_CHECKED)
    out = [plain.SUMMARY_INTRO, plain.STATUS_SENTENCE.get(ev["overall"], "")]

    rest = []
    if m1:
        why = [f"{plain.short(k)} ({_and(names[:2])})" for k, _, names in _kinds(ev, 1, plain.MUST_FIX)[:2]]
        rest.append(f"{m1} need to be fixed" + (f", mainly because of {_and(why)}" if why else ""))
    if d1:
        rest.append(f"{d1} need a decision")
    if n1:
        rest.append(f"{n1} could not be checked")
    out.append(f"In the copy, we checked the {len(i1)} tables the migration uses: "
               f"{_count(i1, plain.CORRECT, plain.BY_DESIGN)} are exact copies"
               + (", but " + "; ".join(rest) if rest else "") + ".")

    rest = [f"{m2} need to be fixed"] if m2 else []
    if d2:
        rest.append(f"{d2} need a decision from the client")
    if n2:
        rest.append(f"{n2} could not be checked")
    out.append(f"In the move into the new system, we checked {len(i2)} mappings: "
               f"{_count(i2, plain.CORRECT, plain.BY_DESIGN)} are correct" + (", " + _and(rest) if rest else "") + ".")
    kinds = _kinds(ev, 2, plain.MUST_FIX)
    if kinds:
        kind, _, names = kinds[0]
        line = f"The biggest problem is {plain.short(kind)}, for example in {_and(names[:3])}"
        if len(kinds) > 1:
            line += f"; other mappings have {_and([plain.short(k) for k, _, _ in kinds[1:4]], 'or')}"
        out.append(line + ".")

    must, decide = m1 + m2, d1 + d2
    if must:
        out.append(f"In total, {must} tables or mappings must be fixed"
                   + (f" and {decide} need a decision" if decide else "") + " before go-live.")
    elif decide:
        out.append(f"In total, {decide} tables or mappings need a decision before go-live.")
    if "stopped early" in (ev["run"].get("mode") or ""):
        out.append("The run was stopped early, so this is a partial report.")
    out.append(plain.SUMMARY_CLOSE)
    return " ".join(x for x in out if x)


def _summary(wb, ev):
    """The first sheet: the whole report in one short paragraph, nothing else."""
    ws = wb[SUMMARY]
    ws.sheet_properties.tabColor = NAVY
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 125
    _title(ws, ev, "Database Migration Check")
    text = _summary_text(ev)
    _put(ws, 5, 1, text, 11, wrap=True)
    ws.row_dimensions[5].height = 16 * max(1, -(-len(text) // 115)) + 8
    _put(ws, 7, 1, f"Run {ev['run'].get('id')}. Read-only check: nothing was changed in any database. No passwords or "
                   "server addresses are in this file.", 8, italic=True, color="7F7F7F")
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


# ---- Full Details: the whole report in words (the tables and mappings are on their own sheets) -------------

def _full(wb, ev):
    ws = wb.create_sheet(FULL)
    ws.sheet_properties.tabColor = "7030A0"
    ws.sheet_view.showGridLines = False
    for c, w in zip("ABCDEF", (28, 24, 18, 18, 18, 30)):
        ws.column_dimensions[c].width = w
    _title(ws, ev, "Full details of the report")
    p1, p2 = ev["part1"], ev["part2"]
    i1, i2 = p1["items"], p2["items"]
    ai = ev.get("summary") or {}
    tdb = _target_db(ev)
    r = 5

    def title(text):
        nonlocal r
        _put(ws, r, 1, text, 13, True, color=NAVY)
        for c in range(1, 7):
            ws.cell(row=r, column=c).border = Border(bottom=Side(style="medium", color=NAVY))
        r += 1

    def text(t, bold=False):
        nonlocal r
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        _put(ws, r, 1, t, bold=bold, wrap=True)
        ws.row_dimensions[r].height = 14 * max(1, -(-len(t) // 125)) + 6
        r += 1

    def point(lead, t):
        text(f"•  {lead}: {t}")

    def header(names):
        nonlocal r
        for c, name in enumerate(names, 1):
            cell = _put(ws, r, c, name, bold=True, color="FFFFFF", fill=NAVY, wrap=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = GRID
        ws.row_dimensions[r].height = 28
        r += 1

    def cells(values, centered=(3, 4, 5)):
        nonlocal r
        for c, v in enumerate(values, 1):
            cell = _put(ws, r, c, "—" if v is None else v, wrap=True)
            cell.border = GRID
            if c in centered:
                cell.alignment = Alignment(horizontal="center", vertical="top")
            if isinstance(v, int) and not isinstance(v, bool):
                cell.number_format = "#,##0"
        r += 1

    def findings(part, what):
        """One point per kind of finding: Need to fix first, then Needs a decision."""
        for result in (plain.MUST_FIX, plain.DECIDE):
            for kind, n, names in _kinds(ev, part, result):
                lead = plain.short(kind)[:1].upper() + plain.short(kind)[1:]
                if result == plain.DECIDE:
                    lead += " (needs a decision)"
                point(lead, f"{n} issue{'s' if n != 1 else ''} in {len(names)} {what}{'s' if len(names) != 1 else ''}, "
                            f"e.g. {_and(names[:4])}.")

    # 1. About
    title("1. About this report")
    text(plain.ABOUT)
    r += 1

    # 2. Overall result
    title("2. Overall result")
    t1, t2 = f"'{P1_TABLES}'!{P1_RESULT_COL}:{P1_RESULT_COL}", f"'{P2_MAPPINGS}'!{P2_RESULT_COL}:{P2_RESULT_COL}"
    must, nc = S(plain.MUST_FIX), S(plain.NOT_CHECKED)
    _put(ws, r, 1, "Status", 11, True)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    _put(ws, r, 2, f"=IF(COUNTIF({t1},\"{must}\")+COUNTIF({t2},\"{must}\")>0,\"NOT READY\","
                   f"IF(COUNTIF({t1},\"{nc}\")+COUNTIF({t2},\"{nc}\")>0,\"INCOMPLETE\",\"READY\"))", 12, True)
    for word in ("NOT READY", "INCOMPLETE", "READY"):
        fill, color = TONE[word]
        ws.conditional_formatting.add(f"B{r}", FormulaRule(formula=[f'B{r}="{word}"'], fill=PatternFill("solid", fgColor=fill),
                                                           font=Font(name=FONT, size=12, bold=True, color=color)))
    r += 1
    items = i1 + i2
    mf, dc = _count(items, plain.MUST_FIX), _count(items, plain.DECIDE)
    text(f"{plain.STATUS_SENTENCE.get(ev['overall'], '')} {mf} tables or mappings need to be fixed and {dc} need a "
         f"decision from the client. {_count(items, plain.CORRECT, plain.BY_DESIGN)} of the {len(items)} tables and "
         "mappings checked are correct.")
    r += 1

    # 3. Table count
    title("3. Table count")
    header(["Server", "Database", "Tables", "Used by the migration", "Not used", "Role"])
    copy_of = {}
    for d in p1.get("databases") or []:
        copy_of[d["target_db"]] = d["source_db"]
        total, unused = d.get("tables_source"), d.get("not_required")
        used = total - unused if total is not None and unused is not None else None
        cells(["ATNM (client)", d["source_db"], total, used, unused, "client database, copy source"])
    for d in (ev.get("coverage") or {}).get("databases") or []:
        total, used = d.get("tables"), d.get("in_plan")
        unused = total - used if total is not None and used is not None else None
        role = ("could not be read in this run" if d.get("error") else "the new system" if d["role"] == "target"
                else f"copy of {copy_of[d['name']]} on RDS" if d["name"] in copy_of else "migration source on RDS")
        cells(["RDS", d["name"], total, used, unused, role])
    missing = [_name(i) for i in i1 if i.get("in_target") is False]
    missing += [t["name"] for i in i2 for t in i.get("target_tables") or [] if not t.get("exists", True)]
    text(f"{len(missing)} tables the migration needs are missing: {_and(missing[:10])}." if missing
         else "No table that the migration needs is missing.")
    text(f"The tables that are not used by the migration are listed in the sheet '{NOT_IN_PLAN}' so the client can "
         "confirm none was forgotten.")
    r += 1

    # 4. Results by step
    title("4. Results by step")
    words = [plain.CORRECT, plain.MUST_FIX, plain.DECIDE, plain.NOT_CHECKED, plain.BY_DESIGN]
    header(["Step", "Checked"] + [S(w) for w in words[:4]])
    for name, sheet, col, n in (("1. Copy (ATNM → RDS)", P1_TABLES, P1_RESULT_COL, f"{len(i1)} tables"),
                                (f"2. Migration (RDS → {tdb})", P2_MAPPINGS, P2_RESULT_COL, f"{len(i2)} mappings")):
        cells([name, n] + [f"=COUNTIF('{sheet}'!{col}:{col},\"{S(w)}\")" for w in words[:4]], centered=(2, 3, 4, 5, 6))
        _link(ws, r - 1, 1, sheet, name)
        ws.cell(row=r - 1, column=1).border = GRID
    if _count(i2, plain.BY_DESIGN):
        text(f"{_count(i2, plain.BY_DESIGN)} mapping(s) are different on purpose (By design) and are counted as correct "
             "in this report.")
    r += 1

    # 5. Section 1
    title("5. Section 1 · Copy: ATNM → RDS")
    per_db = _and([f"{d['required']} from {d['source_db']}" for d in p1.get("databases") or []])
    m1 = _count(i1, plain.MUST_FIX)
    text(f"We checked the {len(i1)} tables the migration uses" + (f": {per_db}" if per_db else "") + ". For each table "
         "we compared the columns, the number of records and every value between the client server and its copy on "
         f"RDS. {_count(i1, plain.CORRECT, plain.BY_DESIGN)} tables are exact copies; {m1} need to be fixed "
         f"({sum(1 for x in ev['issues'] if x['part'] == 1 and x['result'] == plain.MUST_FIX)} issues)"
         + (f"; {_count(i1, plain.DECIDE)} need a decision" if _count(i1, plain.DECIDE) else "")
         + (f"; {_count(i1, plain.NOT_CHECKED)} could not be checked" if _count(i1, plain.NOT_CHECKED) else "") + ".")
    if ai.get("part1_summary"):
        text(ai["part1_summary"])
    findings(1, "table")
    r += 1

    # 6. Section 2
    title(f"6. Section 2 · Migration: RDS → {tdb}")
    text(f"We checked the {len(i2)} mappings (old table → new table) that move the RDS data into the new system. For "
         "each we compared the columns (including renamed ones), the number of records, every value and the links "
         f"between tables. {_count(i2, plain.CORRECT)} are correct, {_count(i2, plain.BY_DESIGN)} are different on "
         f"purpose (by design), {_count(i2, plain.MUST_FIX)} need to be fixed "
         f"({sum(1 for x in ev['issues'] if x['part'] == 2 and x['result'] == plain.MUST_FIX)} issues) and "
         f"{_count(i2, plain.DECIDE)} need a decision "
         f"({sum(1 for x in ev['issues'] if x['part'] == 2 and x['result'] == plain.DECIDE)} questions)"
         + (f"; {_count(i2, plain.NOT_CHECKED)} could not be checked" if _count(i2, plain.NOT_CHECKED) else "") + ".")
    if ai.get("part2_summary"):
        text(ai["part2_summary"])
    findings(2, "mapping")
    r += 1

    # 7. Decisions
    title("7. Decisions needed from the client")
    asks = [x for x in ev["issues"] if x["result"] == plain.DECIDE]
    if asks:
        text(f"{len({x['item'] for x in asks})} tables or mappings have columns or values that were not carried over "
             "as they were. They may have been changed on purpose, so the client must confirm each one:")
        by_item = {}
        for x in asks:
            by_item.setdefault(x["item"], x)
        names = {i["id"]: _name(i) for i in items}
        for item, x in by_item.items():
            finding = x["finding"] if len(x["finding"]) <= 220 else x["finding"][:217] + "..."
            point(names.get(item, x.get("mapping") or x["table"]), finding)
    else:
        text("No decision is needed from the client.")
    r += 1

    # 8. How the check was done
    title("8. How the check was done")
    text(plain.HOW_CHECKED)
    r += 1

    # 9. Notes and limits
    title("9. Notes and limits")
    live = [_name(i) for i in i1 if i.get("live")]
    if live:
        point("Tables in use", f"{_and(live[:5])} changed during the check, so their counts are a snapshot; a final "
                               "check should be run at an agreed cut-off time.")
    if ev.get("not_checked"):
        point("Not checked", f"{len(ev['not_checked'])} parts could not be checked in this run; each is listed with "
                             "its reason on the sheets.")
    designed = [_name(i) for i in i2 if i["result"] == plain.BY_DESIGN]
    if designed:
        point("By design", f"{_and(designed[:5])}: reshaped by business rules, not compared record by record.")
    for lead, t in plain.NOTES:
        if lead != "By design" or not designed:
            point(lead, t)
    r += 1

    # 10. What the status means
    title("10. What the status means")
    for word in plain.RESULTS:
        fill, color = TONE[S(word)]
        _put(ws, r, 1, S(word), bold=True, color=color, fill=fill).border = GRID
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        _put(ws, r, 2, plain.RESULT_HELP[word], wrap=True)
        r += 1
    r += 1
    _put(ws, r, 1, f"Run {ev['run'].get('id')}{' · ' + ev['run']['mode'] if ev['run'].get('mode') else ''}. Read-only "
                   "check: nothing was changed in any database. No passwords or server addresses are in this file.",
         8, italic=True, color="7F7F7F")
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


def build(ev, path):
    wb = Workbook()
    wb.active.title = SUMMARY
    _main1(wb, ev)
    _main2(wb, ev)
    _others(wb, ev)
    _full(wb, ev)
    _summary(wb, ev)
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    wb.save(path)
