"""The words of the report: results, what each kind of finding means, and what to do -
written for a reader who has not worked on the migration. Database names come from the
configuration (backend/.env), never from here."""
from .. import config

# The new system's database, and one source database as an example (DB_*_NAME in .env).
TARGET_DB = config.DATABASES[config.TARGET_SIDE]["name"]
EXAMPLE_DB = config.DATABASES[config.SOURCE_SIDES[0]]["name"]

# Results, most severe first.
MUST_FIX = "Must fix"
NOT_CHECKED = "Not checked"
DECIDE = "Needs a decision"
CORRECT = "Correct"
BY_DESIGN = "By design"
RESULTS = (MUST_FIX, NOT_CHECKED, DECIDE, CORRECT, BY_DESIGN)
ORDER = {r: i for i, r in enumerate(RESULTS)}

RESULT_HELP = {
    MUST_FIX: "Data is missing, different or at risk. Fix it before go-live.",
    NOT_CHECKED: "Could not be checked in this run (the reason is given). Check it again.",
    DECIDE: "Not necessarily wrong, but a person must confirm it (for example a column dropped on purpose).",
    CORRECT: "Checked and found correct.",
    BY_DESIGN: "Not compared one to one on purpose (transformed or excluded data); listed for completeness.",
}

OVERALL_HELP = {
    "NOT READY": "At least one item must be fixed before go-live.",
    "INCOMPLETE": "Nothing must be fixed among what was checked, but some items could not be checked.",
    "READY": "Every item was checked; nothing must be fixed.",
}


# How a result is written in the Excel workbook. The evidence, the Word report and the app keep
# the words above; only the workbook shows these.
SHOWN = {MUST_FIX: "Need to fix"}


def shown(word):
    return SHOWN.get(word, word)


def worst(*results):
    rs = [r for r in results if r and r != BY_DESIGN]
    return min(rs, key=ORDER.get) if rs else BY_DESIGN


def overall(results):
    rs = list(results)
    if MUST_FIX in rs:
        return "NOT READY"
    if NOT_CHECKED in rs:
        return "INCOMPLETE"
    return "READY"


# Kinds of finding: what it means for the business, and what to do about it.
KINDS = {
    "Table": ("The table was not copied, so none of its data is in the new place.",
              "Copy the table, then check again."),
    "Row count": ("Some records were not copied, or extra records appeared.",
                  "Copy the missing records (or remove the extra ones), then check again."),
    "Values": ("Some records have different values after the migration.",
               "Compare the listed records and reload them, then check again."),
    "Columns": ("Information from the source table is not in the target table.",
                "Add the missing columns, or confirm they were dropped on purpose."),
    "Column not migrated": ("A column was not moved to the target table, and no renamed column holds its data.",
                            "Confirm it was dropped on purpose; otherwise map it in the migration plan."),
    "Extra columns": ("The target table has columns the source table did not have.",
                      "Usually expected (new design); confirm they are filled as intended."),
    "Column type": ("A column stores its values in a different format; some values may change or not fit.",
                    "Confirm the target format holds every value."),
    "Renamed column": ("A column may have a new name, but its data does not prove it fully.",
                       "Confirm the rename; if correct, record it in the migration plan."),
    "Primary key": ("Nothing stops the same record from being stored twice.",
                    "Add the primary key in the target table."),
    "Keys & rules": ("The target table does not enforce the same rules (unique records, links, default values).",
                     "Add the missing keys and rules, or confirm they are not needed."),
    "Identity": ("Adding new records will fail, because the next number is already used.",
                 "Reset the identity counter (DBCC CHECKIDENT) before go-live."),
    "Key links": ("Some records point to the wrong related entry, or to none.",
                  "Fix the lookup values or the translation of ids, then reload."),
    "Duplicates": ("Some records appear more than once in the target table.",
                   "Remove the duplicates and find why the load repeated them."),
    "Orphans": ("Some records point to entries that do not exist.",
                "Add the missing entries or correct the links."),
    "Empty values": ("Values that were filled in are now empty.",
                     "Find why the load left them empty, then reload."),
    "Live table": ("The table changed while it was checked; the result is a snapshot.",
                   "Check again at a quiet moment, or at an agreed cutoff."),
    "Other": ("See the finding.", "Investigate the finding."),
}


# The likely cause of each kind of finding, by section (1: the copy ATNM → RDS, 2: the move into the
# new system), for the "Why (likely cause)" column of the workbook. A likely cause, not a proven one.
CAUSES = {
    1: {
        "Table": "Copy issue - the table was not copied to RDS.",
        "Row count": "Copy issue - records were not copied, or extra ones were added; often the client added or "
                     "deleted records after the copy was made.",
        "Values": "Copy issue - records were changed on the client server after the copy, or a value was converted "
                  "during the copy (for example a date or a text).",
        "Columns": "Copy issue - the RDS table was created without these columns (an older table design).",
        "Extra columns": "Copy issue - the RDS table has columns the client's table does not have.",
        "Column type": "Copy issue - the column was created with another type on RDS, so values may be cut or "
                       "converted.",
        "Renamed column": "Copy issue - the column has another name on RDS.",
        "Empty values": "Copy issue - values were not copied into this column, so it is empty on RDS.",
        "Primary key": "Copy issue - the table's key was not copied.",
        "Keys & rules": "Copy issue - the table's keys or rules were not copied.",
        "Identity": "Copy issue - the identity counter was not set after the copy.",
        "Live table": "Timing - the client is still writing to this table, so it changed during the check.",
    },
    2: {
        "Table": "Load issue - the new table does not exist or was not loaded.",
        "Row count": "Load issue - records were not loaded into the new table (the load stopped early or filtered "
                     "them out), or some were loaded twice.",
        "Values": "Conversion issue - values changed when they were moved into the new table (another type, "
                  "format or lookup list).",
        "Columns": "Mapping issue - the new table has no column for this information.",
        "Column not migrated": "Mapping issue - the column was renamed or dropped in the new design, and the "
                               "mapping file does not say which.",
        "Extra columns": "Design change - the new table has columns of its own.",
        "Column type": "Design change - the new table stores the value with another type, so values may be cut, "
                       "rounded or converted.",
        "Renamed column": "Mapping issue - the column seems renamed, but the data does not prove it.",
        "Key links": "Mapping issue - the old code was not translated to the right id of the new list.",
        "Duplicates": "Load issue - the same records were loaded more than once.",
        "Orphans": "Load or mapping issue - records point to entries that were not loaded.",
        "Empty values": "Load or mapping issue - the value was not carried over, so it is empty now.",
        "Primary key": "Design issue - the new table has no primary key.",
        "Keys & rules": "Design issue - the new table is missing a key or rule.",
        "Identity": "Load issue - the identity counter was not reset after the load.",
        "Live table": "Timing - the source changed during the check.",
    },
}
CAUSE_OTHER = "See What we found."
CAUSE_NOT_CHECKED = "Not checked - the check could not run (the reason is in What we found)."
CAUSE_BY_DESIGN = "By design - changed on purpose by the migration's business rules."


def cause(part, kind):
    return CAUSES.get(part, {}).get(kind, CAUSE_OTHER)


# Each kind of finding in a few words, for the sentences of the Excel Summary and Full Details.
KIND_SHORT = {
    "Table": "tables that were not copied",
    "Row count": "missing or extra records",
    "Values": "changed values",
    "Columns": "missing columns",
    "Column not migrated": "columns that were not moved",
    "Extra columns": "extra columns",
    "Column type": "changed column types",
    "Renamed column": "renamed columns not proven by the data",
    "Primary key": "missing primary keys",
    "Keys & rules": "missing keys and rules",
    "Identity": "identity counters left behind",
    "Key links": "wrong or empty links",
    "Duplicates": "duplicate records",
    "Orphans": "records pointing at nothing",
    "Empty values": "values that became empty",
    "Live table": "tables that changed during the check",
    "Other": "other differences",
}


def short(kind):
    return KIND_SHORT.get(kind, KIND_SHORT["Other"])


def meaning(kind):
    return KINDS.get(kind, KINDS["Other"])[0]


def action(kind):
    return KINDS.get(kind, KINDS["Other"])[1]


# ---- the Excel Summary and Full Details sheets -----------------------------------------------------------

SUMMARY_INTRO = (
    "This report checks whether the client's data was moved completely and correctly, in two steps: first the "
    "copy of the client's databases from the ATNM server to RDS, then the move from RDS into the new system, "
    f"{TARGET_DB}.")

STATUS_SENTENCE = {
    "NOT READY": "The migration is not ready for go-live yet.",
    "INCOMPLETE": "Nothing that was checked needs to be fixed, but some parts could not be checked yet.",
    "READY": "The migration is ready: everything that was checked is correct.",
}

SUMMARY_CLOSE = "Every table, with what is wrong and what to do, is listed in the other sheets of this report."

ABOUT = (
    f"This report checks whether the client's data was moved completely and correctly into the new system "
    f"({TARGET_DB}). The data moves in two steps, and both were checked: first the client's databases on the ATNM "
    "server were copied to RDS (Section 1), then the RDS tables were moved into the new system, where many were "
    "renamed and restructured (Section 2). Every number was read directly from the databases during this run; "
    "nothing was changed in any database.")

HOW_CHECKED = (
    "Rows were matched on a key that is unique on both sides (or, without one, compared as whole-row "
    "fingerprints), and every column was compared on every record - not a sample. Renamed columns were accepted "
    "only when their values are identical on every record. Each code that became an id in the new system was "
    "followed to its list to confirm it points at the right entry. All checks only read the databases.")

NOTES = [
    ("By design", "tables reshaped by business rules are not compared record by record."),
    ("Data only", "the report grades the data; database rules such as keys and defaults are not graded."),
]

GLOSSARY = [
    ("Server", "A computer that runs the databases. ATNM is the client's server (reached over VPN); RDS is the "
               "AWS server that holds the copies and the new system."),
    ("Database", f"A collection of tables, e.g. {EXAMPLE_DB}."),
    ("Table", "A list of records, like a sheet in Excel."),
    ("Row / record", "One entry in a table, like one line in a sheet."),
    ("Column", "One kind of information in a table, like one column in a sheet."),
    ("Link (foreign key)", "A column that points to a record in another table (e.g. a task points to its crew type)."),
    ("Renamed column", "The same information under a different column name in the target table; confirmed only when every value is identical."),
    ("Migration plan", "The list of which source tables become which target tables (backend/mappings/migration_plan.yaml)."),
    ("Section 1: ATNM → RDS", "The copy of the client's databases to the RDS server; should be an exact copy."),
    (f"Section 2: RDS → {TARGET_DB}", "The move into the new system; tables are renamed, reshaped and linked."),
]
