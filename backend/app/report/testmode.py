"""Test mode of the report: check only a few small tables, to try the whole report safely.

    REPORT_TEST_TABLES=5    Section 1 checks the 5 smallest required tables (ATNM → RDS) and
                            Section 2 the 5 smallest mappings; everything else in the report
                            works as usual and every file is marked TEST.
    REPORT_TEST_TABLES=0    (or no line) the normal report: every required table and mapping.

The tables are picked from the catalogs' row counts (metadata, nothing is counted): the
smallest ones that have records, both sides present, so the test is light on the shared
servers and still checks real data. Section 1 takes the smallest table of each database pair
first, so every pair is tried, then the smallest of all. No table is named here. The choice is kept with the run, so a resumed
run checks the same ones.
"""
from . import settings

LABEL = "TEST RUN"


def on():
    return settings.TEST_TABLES > 0


def info(plan_tables, plan_mappings):
    """What the preflight shows, or None when test mode is off."""
    if not on():
        return None
    return {"tables": min(settings.TEST_TABLES, plan_tables), "mappings": min(settings.TEST_TABLES, plan_mappings),
            "of_tables": plan_tables, "of_mappings": plan_mappings}


def _smallest(groups, n):
    """Up to `n` items of `groups` (lists sorted smallest first): the first of each group, then
    the smallest of all the rest - so a group with only big items adds just its smallest one."""
    out = [g[0] for g in groups if g][:n]
    rest = sorted(x for g in groups for x in g[1:])
    return sorted(out + rest[:max(0, n - len(out))])


def pick():
    """The tables and mappings of a test run:
    {"part1": {pair id: [keys]}, "part2": [mapping ids], "of_tables", "of_mappings", "text"}.
    Raises RuntimeError (plain words) when a catalog cannot be read."""
    from .. import main
    from ..ATNM import api as atnm_api
    from ..ATNM import required as atnm_required
    from ..ATNM import settings as atnm_settings

    n = settings.TEST_TABLES
    pairs = atnm_settings.PAIRS

    # Section 1: the smallest required tables with records, present on both servers.
    cats = atnm_api._read_all(pairs, refresh=False)
    groups, of_tables = [], 0
    for p in pairs:
        src, tgt = cats[(p.id, "source")], cats[(p.id, "target")]
        for cat in (src, tgt):
            if "error" in cat:
                raise RuntimeError(f"The test tables cannot be picked: {cat['error'].get('message') or cat['error']}")
        req = atnm_required.tables(p)
        of_tables += len(req)
        cands = []
        for key in req:
            s, t = src["tables"].get(key), tgt["tables"].get(key)
            if s and t and s["rows"] > 0:
                cands.append((max(s["rows"], t["rows"]), key, p.id))
        groups.append(sorted(cands))
    part1 = {p.id: [] for p in pairs}
    for _, key, pid in _smallest(groups, n):
        part1[pid].append(key)

    # Section 2: the smallest mappings whose tables all exist, can be read and have records.
    plan = main.get_plan()
    live = main._live_tables(refresh=False)
    cands = []
    for m in plan.mappings:
        members = [main._entry(live, x.ref) for x in m.sources + m.targets]
        if not members or any(e is None or e.get("locked") or e.get("rows") is None for e in members):
            continue
        sources = members[:len(m.sources)]
        if sum(e["rows"] for e in sources) <= 0:
            continue
        cands.append((max(e["rows"] for e in members), m.id))
    part2 = [mid for _, mid in sorted(cands)[:n]]

    sel = {"part1": part1, "part2": part2, "of_tables": of_tables, "of_mappings": len(plan.mappings)}
    sel["text"] = text(sel)
    return sel


def count1(sel):
    return sum(len(v) for v in sel["part1"].values())


def text(sel):
    """The line every part of a test report carries."""
    return (f"{LABEL}: only {count1(sel)} of {sel['of_tables']} required tables (Section 1) and {len(sel['part2'])} of "
            f"{sel['of_mappings']} mappings (Section 2) were checked - the smallest ones. This is not the final result.")


def keep1(sel, pair_id):
    """The Section 1 tables of `pair_id` a test run keeps (lower-case keys), or None: all."""
    return None if not sel else set(sel["part1"].get(pair_id) or [])


def keep2(sel):
    """The Section 2 mappings a test run keeps, or None: all."""
    return None if not sel else set(sel["part2"])
