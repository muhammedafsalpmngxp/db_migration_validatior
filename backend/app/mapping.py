"""The migration plan: loaded from YAML, validated, and indexed by table.

The plan says only which table maps to which. Row counts, columns and types are never
stored here; they are read live from the databases.
"""
from dataclasses import dataclass, field

import yaml

from . import config

TYPES = ("one_to_one", "union", "merge", "transform", "excluded")


class PlanError(ValueError):
    """The mapping file is malformed; the message says where."""


@dataclass(frozen=True)
class TableRef:
    side: str
    schema: str
    table: str

    @property
    def ref(self):
        return f"{self.side}.{self.schema}.{self.table}"

    @property
    def key(self):
        """Case-insensitive identity, the way SQL Server compares names by default."""
        return self.ref.lower()


def parse_ref(text):
    """'A.dbo.Company' -> TableRef('A', 'dbo', 'Company')."""
    parts = str(text).strip().split(".", 2)
    if len(parts) != 3 or not all(parts):
        raise PlanError(f"table reference must be <side>.<schema>.<table>: {text!r}")
    side, schema, table = parts
    side = side.upper()
    if side not in config.DATABASES:
        raise PlanError(f"unknown database side {side!r} in {text!r} (expected A, B or T)")
    return TableRef(side, schema, table)


@dataclass
class Member:
    """One table taking part in a mapping."""
    ref: TableRef
    role: str = None   # merge only: driving | lookup


@dataclass
class Mapping:
    id: str
    type: str
    sources: list
    targets: list
    note: str = None
    columns: dict = field(default_factory=dict)   # declared renames: source col -> target col


@dataclass
class Plan:
    mappings: list
    by_source: dict = field(default_factory=dict)   # TableRef.key -> Mapping
    by_target: dict = field(default_factory=dict)   # TableRef.key -> Mapping
    # Source tables checked only as a copy (ATNM -> RDS), not migrated by any mapping.
    copy_only: list = field(default_factory=list)   # [TableRef]
    # An RDS copy whose table name differs from its ATNM table: TableRef.key -> "schema.table" on ATNM.
    atnm_names: dict = field(default_factory=dict)

    def for_source(self, ref):
        return self.by_source.get(ref.key)

    def scope(self):
        """Every source table in the plan, in file order, with its mapping."""
        return [(m, s) for m in self.mappings for s in m.sources]

    def contains(self, ref):
        return ref.key in self.by_source or ref.key in self.by_target


def _member(item, where):
    if isinstance(item, str):
        return Member(parse_ref(item))
    if not isinstance(item, dict) or "table" not in item:
        raise PlanError(f"{where}: expected a table reference or {{table: ..., role: ...}}")
    return Member(parse_ref(item["table"]), item.get("role"))


def load(path=None):
    path = path or config.MAPPING_FILE
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    mappings, seen_ids = [], set()
    by_source, by_target = {}, {}
    for i, item in enumerate(raw.get("mappings") or []):
        mid = item.get("id")
        if not mid:
            raise PlanError(f"mappings[{i}]: missing id")
        where = f"mapping {mid!r}"
        if mid in seen_ids:
            raise PlanError(f"{where}: duplicate id")
        seen_ids.add(mid)
        kind = item.get("type")
        if kind not in TYPES:
            raise PlanError(f"{where}: type must be one of {', '.join(TYPES)}")

        sources = [_member(s, where) for s in item.get("sources") or []]
        targets = [_member(t, where) for t in item.get("targets") or []]
        if not sources:
            raise PlanError(f"{where}: needs at least one source")
        if kind == "excluded" and targets:
            raise PlanError(f"{where}: an excluded mapping has no targets")
        if kind != "excluded" and not targets:
            raise PlanError(f"{where}: needs at least one target")
        for m in sources:
            if m.ref.side not in config.SOURCE_SIDES:
                raise PlanError(f"{where}: source {m.ref.ref} is not in a source database")
        for m in targets:
            if m.ref.side != config.TARGET_SIDE:
                raise PlanError(f"{where}: target {m.ref.ref} is not in the target database")
        if kind == "one_to_one" and (len(sources) != 1 or len(targets) != 1):
            raise PlanError(f"{where}: one_to_one takes exactly one source and one target")
        if kind == "merge" and sum(m.role == "driving" for m in sources) != 1:
            raise PlanError(f"{where}: merge needs exactly one source with role: driving")

        columns = item.get("columns") or {}
        if not isinstance(columns, dict):
            raise PlanError(f"{where}: columns must map source column names to target names")

        mapping = Mapping(mid, kind, sources, targets, item.get("note"),
                          {str(k): str(v) for k, v in columns.items()})
        for m in sources:
            if m.ref.key in by_source:
                raise PlanError(
                    f"{where}: {m.ref.ref} is already a source of {by_source[m.ref.key].id!r}"
                )
            by_source[m.ref.key] = mapping
        for m in targets:
            by_target.setdefault(m.ref.key, mapping)
        mappings.append(mapping)

    copy_only, seen = [], set()
    for text in raw.get("copy_only") or []:
        ref = parse_ref(text)
        where = f"copy_only {ref.ref}"
        if ref.side not in config.SOURCE_SIDES:
            raise PlanError(f"{where}: is not in a source database")
        if ref.key in by_source:
            raise PlanError(f"{where}: is already a source of {by_source[ref.key].id!r}")
        if ref.key in seen:
            raise PlanError(f"{where}: listed twice")
        seen.add(ref.key)
        copy_only.append(ref)

    atnm_names, taken = {}, set()
    raw_names = raw.get("atnm_names") or {}
    if not isinstance(raw_names, dict):
        raise PlanError("atnm_names must map <side>.<schema>.<table> (RDS) to <schema>.<table> (ATNM)")
    for rds_text, atnm_text in raw_names.items():
        ref = parse_ref(rds_text)
        where = f"atnm_names {ref.ref}"
        if ref.side not in config.SOURCE_SIDES:
            raise PlanError(f"{where}: is not in a source database")
        parts = str(atnm_text).strip().split(".")
        if len(parts) != 2 or not all(parts):
            raise PlanError(f"{where}: the ATNM name must be <schema>.<table>, not {atnm_text!r}")
        if ref.key in atnm_names:
            raise PlanError(f"{where}: listed twice")
        atnm_key = (ref.side, str(atnm_text).strip().lower())
        if atnm_key in taken:
            raise PlanError(f"{where}: ATNM table {atnm_text} is already the copy of another table")
        taken.add(atnm_key)
        atnm_names[ref.key] = str(atnm_text).strip()

    return Plan(mappings, by_source, by_target, copy_only, atnm_names)
