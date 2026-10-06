"""The required tables: the ones the migration uses.

They are the source tables of the migration plan (backend/mappings/migration_plan.yaml,
the same file the RDS section reads), plus its `copy_only` tables - checked as a copy
only, with no mapping - so no table is named here and the list follows the plan. A copy
whose table name differs on RDS is declared under `atnm_names` (atnm_names() /
as_rds_names()). A plan table on side A or B belongs to the database pair whose RDS database is
that side's database (DB_SOURCE_A_NAME / DB_SOURCE_B_NAME).
"""
import threading

from .. import config
from .. import mapping as plan_mod

_cache = {"mtime": None, "plan": None}
_lock = threading.Lock()


def _plan():
    """The migration plan, read again whenever the file changes."""
    with _lock:
        mtime = config.MAPPING_FILE.stat().st_mtime
        if _cache["mtime"] != mtime:
            _cache["plan"] = plan_mod.load()
            _cache["mtime"] = mtime
        return _cache["plan"]


def tables(pair):
    """{schema.table in lower case: {schema, table, mapping}} of the tables `pair` must copy
    (mapping None: a copy_only table). Raises plan_mod.PlanError or OSError when the plan
    cannot be read."""
    sides = {s for s in config.SOURCE_SIDES if config.DATABASES[s]["name"].lower() == pair.target_db.lower()}
    plan = _plan()
    out = {}
    for m in plan.mappings:
        for s in m.sources:
            if s.ref.side in sides:
                out[f"{s.ref.schema}.{s.ref.table}".lower()] = {"schema": s.ref.schema, "table": s.ref.table,
                                                               "mapping": m.id}
    for ref in plan.copy_only:
        if ref.side in sides:
            out[f"{ref.schema}.{ref.table}".lower()] = {"schema": ref.schema, "table": ref.table, "mapping": None}
    return out


def atnm_names(pair):
    """{ATNM schema.table: RDS schema.table} (lower case) of the copies of `pair` whose table
    name differs on the two servers - declared under atnm_names in the migration plan."""
    sides = {s for s in config.SOURCE_SIDES if config.DATABASES[s]["name"].lower() == pair.target_db.lower()}
    plan = _plan()
    out = {}
    for key, atnm in plan.atnm_names.items():
        side, rds = key.split(".", 1)
        if side.upper() in sides:
            out[atnm.lower()] = rds
    return out


def as_rds_names(cat, names):
    """The ATNM catalog `cat` with the tables named in `names` filed under their RDS name, so
    each pairs with its copy like any table of the same name. The entry keeps its own
    schema and table name: every query still reads the real ATNM table."""
    if not names or not cat or "error" in cat:
        return cat
    tables = dict(cat["tables"])
    for atnm, rds in names.items():
        entry = tables.pop(atnm, None)
        if entry is not None:
            tables[rds] = {**entry, "key": rds}
    return {**cat, "tables": tables}
