"""The required tables: the ones the migration uses.

They are the source tables of the migration plan (backend/mappings/migration_plan.yaml,
the same file the RDS section reads), so no table is named here and the list follows
the plan. A plan table on side A or B belongs to the database pair whose RDS database is
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
    """{schema.table in lower case: {schema, table, mapping}} of the tables `pair` must copy.
    Raises plan_mod.PlanError or OSError when the plan cannot be read."""
    sides = {s for s in config.SOURCE_SIDES if config.DATABASES[s]["name"].lower() == pair.target_db.lower()}
    out = {}
    for m in _plan().mappings:
        for s in m.sources:
            if s.ref.side in sides:
                out[f"{s.ref.schema}.{s.ref.table}".lower()] = {"schema": s.ref.schema, "table": s.ref.table,
                                                               "mapping": m.id}
    return out
