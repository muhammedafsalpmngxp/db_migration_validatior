"""The words of the report: results, what each kind of finding means, and what to do -
written for a reader who has not worked on the migration."""

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


def meaning(kind):
    return KINDS.get(kind, KINDS["Other"])[0]


def action(kind):
    return KINDS.get(kind, KINDS["Other"])[1]


GLOSSARY = [
    ("Server", "A computer that runs the databases. ATNM is the client's server (reached over VPN); RDS is the "
               "AWS server that holds the copies and the new system."),
    ("Database", "A collection of tables, e.g. AppMasterDB."),
    ("Table", "A list of records, like a sheet in Excel."),
    ("Row / record", "One entry in a table, like one line in a sheet."),
    ("Column", "One kind of information in a table, like one column in a sheet."),
    ("Primary key", "The column(s) that identify each record uniquely, so no record is stored twice."),
    ("Foreign key / link", "A column that points to a record in another table (e.g. a task points to its crew type)."),
    ("Identity counter", "The number the database gives the next new record."),
    ("Renamed column", "The same information under a different column name in the target table; confirmed only when every value is identical."),
    ("Migration plan", "The list of which source tables become which target tables (backend/mappings/migration_plan.yaml)."),
    ("Section 1: ATNM → RDS", "The copy of the client's databases to the RDS server; should be an exact copy."),
    ("Section 2: RDS → AlTasnimBI", "The move into the new system; tables are renamed, reshaped and linked."),
]
