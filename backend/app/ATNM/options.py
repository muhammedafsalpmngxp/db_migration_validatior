"""Optional settings of the ATNM copy check that belong to tables, read from
ATNM_OPTIONS_FILE (default backend/mappings/atnm.yaml). Without the file nothing changes.

cutoff: a live table keeps changing, so ATNM and RDS are never read at the same moment.
With a cutoff both sides compare only the rows up to one moment, using a column of the
table that says when the row was made (a created / modified date):

    cutoff:
      at: "2026-09-30T00:00:00"          # ISO 8601
      columns:
        AppMasterDB:                     # the ATNM database of the pair
          dbo.ActivityTaskPlan: created_at
"""
import threading
from datetime import datetime

import yaml

from . import settings

_cache = {"mtime": None, "data": {}, "error": None}
_lock = threading.Lock()


def _load():
    """The file's content, read again whenever it changes; {} when there is none."""
    path = settings.OPTIONS_FILE
    with _lock:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            _cache.update(mtime=None, data={}, error=None)
            return _cache
        if _cache["mtime"] != mtime:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(data, dict):
                    raise ValueError("the file must hold a mapping")
                _cache.update(mtime=mtime, data=data, error=None)
            except Exception as exc:
                _cache.update(mtime=mtime, data={}, error=f"{path.name}: {exc}")
        return _cache


def error():
    """Why the options file cannot be used, or None."""
    return _load()["error"]


def cutoff(pair, key):
    """{column, value} for table `key` (lower-case schema.table) of `pair`, or None."""
    c = _load()["data"].get("cutoff") or {}
    at = c.get("at")
    if not at:
        return None
    try:
        # A real moment, written back in one fixed form: the value goes into the SQL text.
        value = datetime.fromisoformat(str(at)).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    for db, tables in (c.get("columns") or {}).items():
        if str(db).lower() != pair.source_db.lower() or not isinstance(tables, dict):
            continue
        for table, column in tables.items():
            if str(table).lower() == key.lower() and column:
                return {"column": str(column), "value": value}
    return None
