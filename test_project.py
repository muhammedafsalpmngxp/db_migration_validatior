"""Tests.

    LLM_PROVIDER=mock pytest -v test_project.py

Validation runs against the real databases named in .env - the two this project exists to
compare - not against planted sample data. The assertions are therefore about invariants
that must hold for any pair of real databases (a table is found in whatever schema it
lives in, a key is only used when both sides have it, a huge table is refused rather than
scanned) rather than about particular rows. Nothing here writes: every statement is a
read, rolled back.

The database tests skip themselves when the server is unreachable; the pure diff tests
always run. The agent loop is exercised with LLM_PROVIDER=mock, so a full run costs
nothing.
"""
import os
import time

import pytest

os.environ.setdefault("LLM_PROVIDER", "mock")

import config  # noqa: E402
import tools  # noqa: E402
from utils import db, differ  # noqa: E402


def _reachable():
    try:
        db.ping(config.dsn_a())
        db.ping(config.dsn_b())
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(),
    reason=f"{config.DB_A_NAME} / {config.DB_B_NAME} not reachable - check .env",
)


@pytest.fixture(scope="module")
def live():
    """Both real schemas, paired, with the structural diff. Read once for every test."""
    raw_a = db.fetch_schema(config.dsn_a(), config.SCHEMA)
    raw_b = db.fetch_schema(config.dsn_b(), config.SCHEMA)
    paired_a, paired_b = differ.pair_by_name(raw_a, raw_b)
    return {
        "raw_a": raw_a,
        "raw_b": raw_b,
        "a": paired_a,
        "b": paired_b,
        "diff": differ.diff_schemas(paired_a, paired_b),
    }


# ---------------------------------------------------------------- pure logic


def test_diff_rows_detects_missing_and_modified():
    a = [{"id": 1, "name": "x", "v": 10}, {"id": 2, "name": "y", "v": 20}]
    b = [{"id": 1, "name": "x", "v": 11}, {"id": 3, "name": "z", "v": 30}]
    res = differ.diff_rows(a, b, ["id"])
    assert res["only_in_a_count"] == 1 and res["only_in_a"] == [{"id": 2}]
    assert res["only_in_b_count"] == 1 and res["only_in_b"] == [{"id": 3}]
    assert res["modified_count"] == 1
    assert res["modified"][0]["changes"]["v"] == {"a": 10, "b": 11}
    assert res["identical"] is False


def test_diff_rows_identical():
    rows = [{"id": 1, "v": 1.0}]
    assert differ.diff_rows(rows, [{"id": 1, "v": 1}], ["id"])["identical"] is True


def test_diff_rows_requires_primary_key():
    with pytest.raises(ValueError):
        differ.diff_rows([], [], [])


def test_diff_schemas_finds_every_structural_change():
    a = {
        "t": {
            "columns": {
                "id": {"position": 1, "type": "integer", "nullable": False, "default": None},
                "gone": {"position": 2, "type": "text", "nullable": True, "default": None},
                "amt": {"position": 3, "type": "numeric(10,2)", "nullable": False, "default": None},
            },
            "primary_key": ["id"],
            "indexes": [{"name": "i_a", "definition": ""}],
            "row_count": 5,
        },
        "only_a": {"columns": {}, "primary_key": [], "indexes": [], "row_count": 0},
    }
    b = {
        "t": {
            "columns": {
                "id": {"position": 1, "type": "integer", "nullable": False, "default": None},
                "added": {"position": 2, "type": "text", "nullable": True, "default": None},
                "amt": {
                    "position": 3, "type": "double precision", "nullable": True, "default": None
                },
            },
            "primary_key": ["id"],
            "indexes": [{"name": "i_b", "definition": ""}],
            "row_count": 7,
        },
        "only_b": {"columns": {}, "primary_key": [], "indexes": [], "row_count": 0},
    }
    d = differ.diff_schemas(a, b)
    assert d["tables_only_in_a"] == ["only_a"] and d["tables_only_in_b"] == ["only_b"]
    t = d["table_diffs"]["t"]
    assert t["columns_only_in_a"] == ["gone"] and t["columns_only_in_b"] == ["added"]
    assert t["type_changes"][0]["column"] == "amt"
    assert t["nullability_changes"][0]["b"] == "NULL"
    assert t["indexes_only_in_a"] == ["i_a"] and t["indexes_only_in_b"] == ["i_b"]
    assert d["row_count_mismatches"][0] == {"table": "t", "a": 5, "b": 7}


# ---------------------------------------------------------------- the real databases


@needs_db
def test_both_databases_are_reachable_and_not_empty(live):
    assert live["raw_a"], f"{config.DB_A_NAME} reported no tables"
    assert live["raw_b"], f"{config.DB_B_NAME} reported no tables"


@needs_db
def test_every_schema_is_read_not_just_dbo(live):
    """A real database spreads its tables around; reading only dbo would hide most of B."""
    schemas_b = {e["schema"] for e in live["raw_b"].values()}
    assert len(schemas_b) > 1, f"expected several schemas in {config.DB_B_NAME}"
    for key, entry in live["raw_b"].items():
        assert key == f"{entry['schema']}.{entry['table']}"


@needs_db
def test_row_counts_come_from_metadata_not_a_scan(live):
    """count(*) on a table of tens of millions of rows is what used to take minutes."""
    counted = [e for e in live["raw_a"].values() if e["row_count"] is not None]
    assert counted, "no row counts were read at all"
    biggest = max(e["row_count"] for e in counted)
    assert biggest > 1_000_000, "expected a large table in the baseline database"

    started = time.time()
    db.fetch_schema(config.dsn_a(), config.SCHEMA)
    assert time.time() - started < 30, "introspection is scanning, not reading metadata"


@needs_db
def test_a_table_pairs_across_different_schemas(live):
    """The two databases do not agree on where a table lives; pairing is by name."""
    cross = [
        (live["a"][k]["qualified"], live["b"][k]["qualified"])
        for k in set(live["a"]) & set(live["b"])
        if live["a"][k]["schema"] != live["b"][k]["schema"]
    ]
    assert cross, "expected at least one table paired across two different schemas"
    for qa, qb in cross:
        assert qa.split(".", 1)[0] != qb.split(".", 1)[0]


@needs_db
def test_an_explicit_pick_settles_an_ambiguous_name(live):
    """A name in several schemas of one database is ambiguous until the user picks one."""
    by_name = {}
    for key, entry in live["raw_b"].items():
        by_name.setdefault(entry["table"], []).append(key)
    ambiguous = {n: q for n, q in by_name.items() if len(q) > 1}
    if not ambiguous:
        pytest.skip("no duplicate table names in the candidate database")

    name, qualifieds = next(iter(ambiguous.items()))

    def paired_with(chosen):
        pairs, _, _ = differ.match_tables(live["raw_a"], live["raw_b"], chosen_b=chosen)
        return {p["b"] for p in pairs}

    # Ambiguous on its own: neither candidate is matched, because nothing says which.
    assert not (set(qualifieds) & paired_with(None)), f"{name} paired without being asked"
    # Picked: that object now represents the name - if A has it at all.
    if any(e["table"] == name or differ.normalize(e["table"]) == differ.normalize(name)
           for e in live["raw_a"].values()):
        assert qualifieds[0] in paired_with({qualifieds[0]})


@needs_db
def test_row_key_agrees_with_what_the_tool_will_do(live):
    """The summary must not advertise a key the row comparison cannot use."""
    shared = {"schema": {"a": live["a"], "b": live["b"]}, "schema_diff": live["diff"]}
    checked = 0
    for table in live["diff"]["common_tables"]:
        table_diff = live["diff"]["table_diffs"][table]
        if differ.row_key(table_diff):
            continue
        text, raw = tools.execute_tool("compare_table_data", {"table": table}, shared)
        assert raw["error"] == "no usable key", table
        assert "cannot be compared row by row" in text
        checked += 1
        if checked == 3:
            break
    if not checked:
        pytest.skip("every common table has a usable key")


@needs_db
def test_a_huge_table_is_refused_instead_of_scanned(live):
    """Reading both sides of a 70M row table in full would never finish."""
    shared = {"schema": {"a": live["a"], "b": live["b"]}, "schema_diff": live["diff"]}
    huge = [
        t for t in live["diff"]["common_tables"]
        if differ.row_key(live["diff"]["table_diffs"][t])
        and max(
            c for c in (
                live["diff"]["table_diffs"][t]["row_count_a"],
                live["diff"]["table_diffs"][t]["row_count_b"],
            ) if c is not None
        ) > config.ROW_COMPARE_MAX_ROWS
    ]
    if not huge:
        pytest.skip("no common table over the row comparison limit")

    started = time.time()
    text, raw = tools.execute_tool("compare_table_data", {"table": huge[0]}, shared)
    assert raw["error"] == "too large for a row comparison"
    assert "run_sql" in text
    assert time.time() - started < 5, "the guard should answer without touching the rows"


@needs_db
def test_a_row_comparison_runs_where_a_key_exists(live):
    """The happy path: a keyed table of a sane size is compared row by row."""
    shared = {"schema": {"a": live["a"], "b": live["b"]}, "schema_diff": live["diff"]}
    candidates = []
    for t in live["diff"]["common_tables"]:
        d = live["diff"]["table_diffs"][t]
        counts = [c for c in (d["row_count_a"], d["row_count_b"]) if c is not None]
        if differ.row_key(d) and counts and max(counts) <= config.ROW_COMPARE_MAX_ROWS:
            candidates.append((max(counts), t))
    if not candidates:
        pytest.skip("no comparable common table in these databases")

    _, table = min(candidates)
    text, res = tools.execute_tool("compare_table_data", {"table": table}, shared)
    assert res["primary_key"], table
    assert res["rows_a"] >= 0 and res["rows_b"] >= 0
    assert "Row comparison of" in text
    # The objects compared are named, so a cross-schema pair is auditable.
    assert res["object_a"].count(".") == 1 and res["object_b"].count(".") == 1


@needs_db
def test_sampling_a_huge_table_is_quick(live):
    """It reads TOP (n): slow here meant blocking, which dirty reads fixed."""
    biggest = max(live["raw_a"].values(), key=lambda e: e["row_count"] or 0)
    started = time.time()
    rows = db.fetch_rows(config.dsn_a(), biggest["table"], biggest["schema"], limit=10)
    assert len(rows) <= 10
    assert time.time() - started < 20


@needs_db
def test_run_sql_resolves_a_bare_table_name_per_database(live):
    """One query, two databases, two different schemas - resolved on each side."""
    shared = {"schema": {"a": live["a"], "b": live["b"]}}
    cross = [
        k for k in set(live["a"]) & set(live["b"])
        if live["a"][k]["schema"] != live["b"][k]["schema"]
    ]
    if not cross:
        pytest.skip("no cross-schema pair to resolve")

    table = cross[0]
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    for side in ("a", "b"):
        resolved, _ = tools._resolve_sql(sql, shared, side)
        entry = live[side][table]
        assert f"[{entry['schema']}].[{entry['table']}]" in resolved


@needs_db
def test_run_sql_refuses_anything_that_is_not_one_select(live):
    biggest = max(live["raw_a"].values(), key=lambda e: e["row_count"] or 0)
    target = f"{db.quote_ident(biggest['schema'])}.{db.quote_ident(biggest['table'])}"
    for bad in (
        f"DELETE FROM {target}",
        f"SELECT 1; DROP TABLE {target}",
        f"UPDATE {target} SET x = 1",
        "EXEC sp_who",
    ):
        with pytest.raises(ValueError):
            db.run_select(config.dsn_a(), bad)

    rows = db.run_select(config.dsn_a(), f"SELECT TOP (3) * FROM {target}")
    assert len(rows) <= 3


@needs_db
def test_agent_matching_is_only_asked_when_nothing_was_selected(live):
    """An explicit pick is an instruction, not a starting point for the model."""
    from nodes import MatchTablesNode

    shared = {"raw_schema": {"a": live["raw_a"], "b": live["raw_b"]}, "tables": None}
    assert MatchTablesNode().prep(shared)["ask_llm"] is True

    shared["tables"] = [next(iter(live["raw_a"]))]
    assert MatchTablesNode().prep(shared)["ask_llm"] is False


@needs_db
def test_full_flow_end_to_end_on_the_real_databases():
    """The whole graph against the real pair, narrowed to keep it quick and free."""
    from flow import run_comparison

    raw_a = db.fetch_schema(config.dsn_a(), config.SCHEMA)
    raw_b = db.fetch_schema(config.dsn_b(), config.SCHEMA)
    paired_a, paired_b = differ.pair_by_name(raw_a, raw_b)
    diff = differ.diff_schemas(paired_a, paired_b)
    comparable = [t for t in diff["common_tables"] if differ.row_key(diff["table_diffs"][t])]
    if not comparable:
        pytest.skip("no comparable common table in these databases")

    shared = run_comparison("What changed?", tables=comparable[:2])

    assert shared["failed"] is False
    assert shared["table_matches"]["pairs"], "nothing paired"
    assert shared["schema_diff"]["common_tables"]
    assert shared["history"], "the agent made no tool call"
    assert shared["report"].startswith("#")
    assert shared["reviews"][-1]["approve"] is True
    assert shared["llm_stats"]["calls"] > 0
    # A tool call the agent worked around is expected against real data; a failure that
    # cost the run is not.
    blocking = [e for e in shared["errors"] if not e.get("recovered")]
    assert blocking == [], blocking


@needs_db
def test_a_failing_tool_call_becomes_an_observation(live):
    """A tool error has to keep the run alive: the agent picks another approach."""
    from nodes import ExecuteToolNode

    shared = {
        "schema": {"a": live["a"], "b": live["b"]},
        "schema_diff": {"common_tables": [], "table_diffs": {}},
        "history": [],
        "next_action": {
            "tool": "compare_table_data",
            "params": {"table": "does_not_exist_anywhere"},
            "thinking": "",
            "reason": "probe",
        },
    }
    ExecuteToolNode().run(shared)
    assert "TOOL ERROR" in shared["history"][0]["observation"]


# ---------------------------------------------------------------- openai wiring
# These stub the SDK, so they check the plumbing without spending a single token.


def _fake_openai_module(recorder, reject_max_tokens=False):
    import types

    class _Resp:
        def __init__(self):
            self.choices = [
                types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))
            ]

    class _Completions:
        def create(self, **kwargs):
            recorder.append(kwargs)
            if reject_max_tokens and "max_tokens" in kwargs:
                raise TypeError("Unsupported parameter: 'max_tokens' is not supported")
            return _Resp()

    class _OpenAI:
        def __init__(self, **kwargs):
            recorder.append({"client": kwargs})
            self.chat = types.SimpleNamespace(completions=_Completions())

    module = types.ModuleType("openai")
    module.OpenAI = _OpenAI
    return module


def test_openai_provider_sends_the_prompt_and_model(monkeypatch):
    import sys

    from utils import call_llm as llm

    calls = []
    monkeypatch.setitem(sys.modules, "openai", _fake_openai_module(calls))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert llm.call_llm("hello?") == "ok"
    request = calls[-1]
    assert request["model"] == "gpt-4o"
    assert request["messages"][-1] == {"role": "user", "content": "hello?"}


def test_openai_falls_back_to_max_completion_tokens(monkeypatch):
    """Reasoning models reject max_tokens; the wrapper retries with the new parameter."""
    import sys

    from utils import call_llm as llm

    calls = []
    monkeypatch.setitem(
        sys.modules, "openai", _fake_openai_module(calls, reject_max_tokens=True)
    )
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "o4-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert llm.call_llm("hello?") == "ok"
    assert "max_completion_tokens" in calls[-1]


def test_openai_without_a_key_says_so(monkeypatch):
    from utils import call_llm as llm

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        llm.call_llm("hello?")


# ---------------------------------------------------------------- row encoding (TOON)


def test_uniform_rows_name_their_columns_once():
    from utils import toon

    rows = [{"id": 1, "name": "Anita", "qty": 3}, {"id": 2, "name": "Ben", "qty": None}]
    encoded = toon.encode(rows, name="sample")
    assert encoded.splitlines()[0] == "sample[2]{id,name,qty}:"
    assert encoded.splitlines()[1] == "  1,Anita,3"
    assert encoded.splitlines()[2] == "  2,Ben,"  # a missing value is empty, not "None"
    # The point of the format: the column names appear once, not once per row.
    assert encoded.count("name") == 1


def test_values_that_would_break_the_table_are_quoted():
    from utils import toon

    rows = [{"a": "x,y", "b": 'say "hi"', "c": "line1\nline2"}]
    body = toon.encode(rows).splitlines()[1]
    assert body.count(",") >= 3  # the separators, plus the quoted comma inside the value
    assert '"x,y"' in body
    assert '""hi""' in body
    assert "\n" not in body, "a newline inside a value must not start a new row"


def test_rows_with_different_fields_fall_back_to_a_list():
    from utils import toon

    rows = [{"id": 1, "name": "x"}, {"id": 2, "other": "y"}]
    encoded = toon.encode(rows)
    assert "fields differ" in encoded
    assert encoded.count("- ") == 2


def test_empty_and_clipping_behave():
    from utils import toon

    assert toon.encode([]) == "rows[0]: none"

    rows = [{"id": i, "v": "x" * 50} for i in range(40)]
    encoded = toon.encode(rows)
    clipped = toon.clip(encoded, 300)
    assert len(clipped) < len(encoded)
    assert "omitted" in clipped
    # It cuts between rows, never through one.
    for line in clipped.splitlines()[1:-1]:
        assert line.startswith("  ")


def test_a_long_cell_is_trimmed_not_the_row():
    from utils import toon

    rows = [{"id": 1, "blob": "y" * 500, "tail": "kept"}]
    body = toon.encode(rows).splitlines()[1]
    assert "..." in body
    assert body.endswith(",kept"), "trimming one cell must not lose the columns after it"


# ---------------------------------------------------------------- loop efficiency


def test_an_identical_call_is_answered_from_history_not_run_again():
    """A repeat costs a step and a full prompt, and cannot tell the agent anything new."""
    from nodes import ExecuteToolNode

    calls = []

    def fake_tool(tool, params, shared):
        calls.append((tool, params))
        return "first answer", {"n": 1}

    import tools as tools_module

    original = tools_module.execute_tool
    tools_module.execute_tool = fake_tool
    try:
        shared = {"history": [], "next_action": {
            "tool": "run_sql", "params": {"sql": "SELECT 1", "database": "both"},
            "thinking": "", "reason": "probe",
        }}
        ExecuteToolNode().run(shared)
        # Same params, written in a different order: still the same call.
        shared["next_action"] = {
            "tool": "run_sql", "params": {"database": "both", "sql": "SELECT 1"},
            "thinking": "", "reason": "again",
        }
        ExecuteToolNode().run(shared)
    finally:
        tools_module.execute_tool = original

    assert len(calls) == 1, "the tool ran twice for the same call"
    assert shared["history"][1]["repeated"] is True
    assert "already ran this exact call at step 1" in shared["history"][1]["observation"]
    assert "first answer" in shared["history"][1]["observation"]


def test_a_different_call_still_runs():
    from nodes import ExecuteToolNode

    calls = []
    import tools as tools_module

    original = tools_module.execute_tool
    tools_module.execute_tool = lambda t, p, s: (calls.append((t, p)), ("ok", {}))[1]
    try:
        shared = {"history": [], "next_action": {
            "tool": "run_sql", "params": {"sql": "SELECT 1"}, "thinking": "", "reason": "",
        }}
        ExecuteToolNode().run(shared)
        shared["next_action"] = {
            "tool": "run_sql", "params": {"sql": "SELECT 2"}, "thinking": "", "reason": "",
        }
        ExecuteToolNode().run(shared)
    finally:
        tools_module.execute_tool = original

    assert len(calls) == 2
    assert shared["history"][1].get("repeated") is False


def test_a_sql_error_carries_a_hint_the_agent_can_act_on():
    """The raw ODBC text says what is wrong, not what to do - so it gets retried verbatim."""
    from tools import _sql_hint

    reserved = "[42000] Incorrect syntax near the keyword 'RowCount'. (156)"
    assert "reserved" in _sql_hint(reserved) and "[RowCount]" in _sql_hint(reserved)

    assert "sample_rows" in _sql_hint("[42S22] Invalid column name 'columnname'.")
    assert "own schema" in _sql_hint("[42S02] Invalid object name 'ActivityTaskPlan'.")
    assert _sql_hint("some error nobody anticipated") == ""


def test_the_decide_prompt_puts_the_stable_part_first():
    """Prefix caching only pays while the prefix is identical, step after step."""
    from nodes import DECIDE_PROMPT

    catalog = DECIDE_PROMPT.index("{catalog}")
    rules = DECIDE_PROMPT.index("## HOW TO CHOOSE")
    question = DECIDE_PROMPT.index("{question}")
    diff = DECIDE_PROMPT.index("{schema_summary}")
    history = DECIDE_PROMPT.index("{history}")

    assert catalog < rules < question < diff < history, "volatile content must come last"


# ---------------------------------------------------------------- SQL column check


def _schema_for(columns, table="orders", schema="dbo"):
    entry = {
        "schema": schema,
        "table": table,
        "qualified": f"{schema}.{table}",
        "columns": {c: {} for c in columns},
        "primary_key": [],
        "indexes": [],
        "row_count": 0,
    }
    return {"schema": {"a": {table: entry}, "b": {table: entry}}}


def test_a_column_that_does_not_exist_is_caught_before_the_query_runs():
    shared = _schema_for(["id", "status", "amount"])
    assert tools._unknown_columns("SELECT id, statuss FROM orders", shared, "a") == ["statuss"]
    assert tools._unknown_columns("SELECT o.total FROM orders AS o", shared, "a") == ["total"]


def test_valid_sql_is_not_flagged():
    """A false positive blocks a good query, which is worse than letting a bad one run."""
    shared = _schema_for(["id", "status", "amount", "created"])
    for sql in (
        "SELECT id, status FROM orders",
        "SELECT COUNT(*) AS n FROM orders GROUP BY status",
        "SELECT YEAR(created) AS yr, SUM(amount) AS total FROM orders GROUP BY YEAR(created)",
        "SELECT o.id FROM orders AS o WHERE o.status IS NOT NULL",
        "SELECT o.id FROM orders o WHERE o.amount BETWEEN 1 AND 10",
        "SELECT TOP (5) id FROM orders ORDER BY amount DESC",
        "SELECT id FROM orders WHERE status = 'not_a_column_name'",
        "SELECT CAST(amount AS decimal(10,2)) AS x FROM orders",
        "SELECT [status] FROM orders",
        "WITH c AS (SELECT id FROM orders) SELECT id FROM c",
    ):
        assert tools._unknown_columns(sql, shared, "a") == [], sql


def test_the_check_stays_quiet_when_it_does_not_know_the_table():
    """Unknown table means unknown columns: guessing there would refuse valid queries."""
    shared = _schema_for(["id"])
    assert tools._unknown_columns("SELECT anything FROM some_other_table", shared, "a") == []


def test_the_refusal_names_the_columns_that_do_exist():
    shared = _schema_for(["id", "status", "amount"])
    shared["schema_diff"] = {"common_tables": [], "table_diffs": {}}
    with pytest.raises(ValueError) as caught:
        tools.execute_tool(
            "run_sql", {"database": "A", "sql": "SELECT statuss FROM orders"}, shared
        )
    message = str(caught.value)
    assert "statuss is not a column" in message
    assert "amount, id, status" in message
    assert "sample_rows" in message


# ---------------------------------------------------------------- configuration source


def test_the_projects_env_file_beats_the_machine(monkeypatch):
    """A stale key exported by the machine must not silently win over .env."""
    import importlib

    from dotenv import dotenv_values

    on_disk = dotenv_values(os.path.join(os.path.dirname(config.__file__), ".env"))
    if not on_disk.get("OPENAI_API_KEY"):
        pytest.skip("no OPENAI_API_KEY in .env to compare against")

    monkeypatch.setenv("OPENAI_API_KEY", "exported-by-the-machine")
    monkeypatch.delenv("DBCOMPARE_ENV_LOADED", raising=False)
    importlib.reload(config)
    assert os.environ["OPENAI_API_KEY"] == on_disk["OPENAI_API_KEY"]

    # ...but a value set afterwards - the Streamlit sidebar - survives a reload.
    monkeypatch.setenv("OPENAI_API_KEY", "typed-into-the-sidebar")
    importlib.reload(config)
    assert os.environ["OPENAI_API_KEY"] == "typed-into-the-sidebar"


# ---------------------------------------------------------------- SQL type check


def test_a_numeric_aggregate_over_text_is_refused_with_both_types():
    """The type drift is the finding: say it instead of scanning and failing."""
    a = {
        "schema": "dbo", "table": "plan", "qualified": "dbo.plan",
        "columns": {"weightage": {"type": "nvarchar(100)"}, "qty": {"type": "int"}},
        "primary_key": [], "indexes": [], "row_count": 0,
    }
    b = dict(a, columns={"weightage": {"type": "float"}, "qty": {"type": "int"}})
    shared = {"schema": {"a": {"plan": a}, "b": {"plan": b}}}

    problems = tools._type_problems("SELECT SUM(weightage) FROM plan", shared, "a")
    assert len(problems) == 1
    assert "nvarchar(100)" in problems[0] and "float in the other database" in problems[0]
    assert "TRY_CAST" in problems[0]

    # Numeric on this side, and the aggregates T-SQL accepts on text, are left alone.
    assert tools._type_problems("SELECT SUM(weightage) FROM plan", shared, "b") == []
    assert tools._type_problems("SELECT SUM(qty) FROM plan", shared, "a") == []
    assert tools._type_problems("SELECT MAX(weightage) FROM plan", shared, "a") == []
    assert tools._type_problems("SELECT COUNT(weightage) FROM plan", shared, "a") == []


# ---------------------------------------------------------------- SQL scan guard


def _big_table(rows, indexes=(), pk=()):
    entry = {
        "schema": "dbo", "table": "plan", "qualified": "dbo.plan",
        "columns": {"project_id": {"type": "int"}, "note": {"type": "nvarchar(100)"}},
        "primary_key": list(pk),
        "indexes": [{"name": n, "definition": f"NONCLUSTERED ({c}) ON plan"}
                    for n, c in indexes],
        "row_count": rows,
    }
    return {"schema": {"a": {"plan": entry}, "b": {"plan": entry}}}


def test_an_unbounded_aggregate_over_a_huge_table_is_refused():
    shared = _big_table(71_000_000, indexes=[("ix_p", "project_id")])
    problem = tools._scan_problem(
        "SELECT project_id, COUNT(*) FROM plan GROUP BY project_id", shared, "a"
    )
    assert problem and "71,000,000 rows" in problem
    assert "project_id" in problem, "it should name a column worth filtering on"
    assert "force: true" in problem


def test_a_bounded_or_small_query_is_left_alone():
    big = _big_table(71_000_000)
    assert tools._scan_problem(
        "SELECT COUNT(*) FROM plan WHERE project_id = 3", big, "a"
    ) is None
    assert tools._scan_problem("SELECT TOP (5) note FROM plan", big, "a") is None

    small = _big_table(1000)
    assert tools._scan_problem("SELECT COUNT(*) FROM plan", small, "a") is None


def test_the_advice_is_honest_when_there_is_no_index():
    shared = _big_table(71_000_000)  # no indexes, no primary key
    problem = tools._scan_problem("SELECT COUNT(*) FROM plan", shared, "a")
    assert "no index to filter on" in problem


def test_indexed_columns_are_read_from_the_index_definitions():
    entry = _big_table(10, indexes=[("ix_a", "code"), ("ix_b", "project_id, created")],
                       pk=["row_id"])["schema"]["a"]["plan"]
    assert tools._indexed_columns(entry) == ["code", "project_id", "row_id"]


# ---------------------------------------------------------------- run_select shapes


@needs_db
def test_agent_sql_runs_as_written(live):
    """The runner used to wrap queries in a derived table, which imposed rules the agent
    had no reason to expect: unaliased expressions and ORDER BY both failed."""
    biggest = max(live["raw_a"].values(), key=lambda e: e["row_count"] or 0)
    small = min(
        (e for e in live["raw_a"].values() if (e["row_count"] or 0) > 0),
        key=lambda e: e["row_count"],
    )
    target = f"{db.quote_ident(small['schema'])}.{db.quote_ident(small['table'])}"
    column = next(iter(small["columns"]))
    quoted = db.quote_ident(column)

    unaliased = db.run_select(config.dsn_a(), f"SELECT COUNT(*) FROM {target}", limit=5)
    assert unaliased and list(unaliased[0])[0].startswith("column_")

    grouped = db.run_select(
        config.dsn_a(),
        f"SELECT {quoted}, COUNT(*) FROM {target} GROUP BY {quoted}",
        limit=3,
    )
    assert grouped and len(grouped) <= 3

    ordered = db.run_select(
        config.dsn_a(), f"SELECT TOP (3) {quoted} FROM {target} ORDER BY {quoted} DESC", limit=3
    )
    assert len(ordered) <= 3
    assert biggest["row_count"] > 0  # the fixture is real data, not a stub


@needs_db
def test_the_row_cap_is_applied_while_reading(live):
    small = min(
        (e for e in live["raw_a"].values() if (e["row_count"] or 0) > 10),
        key=lambda e: e["row_count"],
    )
    target = f"{db.quote_ident(small['schema'])}.{db.quote_ident(small['table'])}"
    assert len(db.run_select(config.dsn_a(), f"SELECT * FROM {target}", limit=4)) == 4


# ---------------------------------------------------------------- not worth a step


def test_a_table_no_tool_can_help_with_is_marked_answered():
    """Marking it is what stops the agent spending a step per table finding out."""
    diff = {
        "tables_only_in_a": [], "tables_only_in_b": [],
        "common_tables": ["keyed", "keyless", "huge"],
        "tables_with_schema_changes": [],
        "row_count_mismatches": [],
        "table_diffs": {
            "keyed": {
                "columns_only_in_a": [], "columns_only_in_b": [], "type_changes": [],
                "nullability_changes": [], "default_changes": [],
                "primary_key_a": ["id"], "primary_key_b": ["id"],
                "primary_key_changed": False, "indexes_only_in_a": [],
                "indexes_only_in_b": [], "row_count_a": 10, "row_count_b": 11,
                "identical": True,
            },
            "keyless": {
                "columns_only_in_a": [], "columns_only_in_b": [], "type_changes": [],
                "nullability_changes": [], "default_changes": [],
                "primary_key_a": [], "primary_key_b": [],
                "primary_key_changed": False, "indexes_only_in_a": [],
                "indexes_only_in_b": [], "row_count_a": 10, "row_count_b": 11,
                "identical": True,
            },
            "huge": {
                "columns_only_in_a": [], "columns_only_in_b": [], "type_changes": [],
                "nullability_changes": [], "default_changes": [],
                "primary_key_a": ["id"], "primary_key_b": ["id"],
                "primary_key_changed": False, "indexes_only_in_a": [],
                "indexes_only_in_b": [], "row_count_a": 71_000_000, "row_count_b": 5,
                "identical": True,
            },
        },
    }
    lines = differ.summarize_schema_diff(diff, row_compare_max=200_000).splitlines()
    by_table = {ln.strip().split(" [")[0]: ln for ln in lines if ln.startswith("  ")}

    assert "ANSWERED" not in by_table["keyed"], "a comparable table must stay on the list"
    assert "ANSWERED" in by_table["keyless"]
    assert "ANSWERED (too large" in by_table["huge"]
    # The meaning is stated once, not repeated on every line.
    assert sum(ln.count("no tool call can add to it") for ln in lines) == 1


def test_circling_the_same_impossible_table_is_called_out():
    shared = {}
    assert tools._refused(shared, "ActivityTaskPlan") == ""
    second = tools._refused(shared, "ActivityTaskPlan")
    assert "attempt 2 on ActivityTaskPlan" in second
    assert "finish" in second
    # A different table starts its own count.
    assert tools._refused(shared, "task_daily") == ""


# ---------------------------------------------------------------- selective filters


def _huge(indexes=(("ix_p", "project_id"), ("ix_r", "row_id"))):
    entry = {
        "schema": "dbo", "table": "plan", "qualified": "dbo.plan",
        "columns": {c: {"type": "int"} for c in
                    ("project_id", "row_id", "schedule_id", "created_at")},
        "primary_key": [],
        "indexes": [{"name": n, "definition": f"NONCLUSTERED ({c}) ON plan"}
                    for n, c in indexes],
        "row_count": 71_000_000,
    }
    return {"schema": {"a": {"plan": entry}, "b": {"plan": entry}}}


def test_a_where_that_excludes_nothing_is_still_a_full_scan():
    """`WHERE col IS NOT NULL` satisfied the first version of this guard and scanned 6.3M
    rows anyway."""
    shared = _huge()
    for sql in (
        "SELECT COUNT(*), schedule_id FROM plan WHERE schedule_id IS NOT NULL GROUP BY schedule_id",
        "SELECT COUNT(*) FROM plan WHERE created_at > 2026",  # real filter, unindexed
        "SELECT project_id, COUNT(*) FROM plan GROUP BY project_id",  # no WHERE at all
    ):
        problem = tools._scan_problem(sql, shared, "a")
        assert problem, sql
        assert "project_id" in problem  # it names what to filter on instead


def test_a_filter_the_server_can_seek_on_is_allowed_through():
    shared = _huge()
    for sql in (
        "SELECT COUNT(*) FROM plan WHERE project_id = 5",
        "SELECT COUNT(*) FROM plan WHERE row_id BETWEEN 1 AND 1000",
        "SELECT COUNT(*) FROM plan WHERE project_id IN (1, 2, 3)",
        "SELECT COUNT(*) FROM plan WHERE row_id > 900 AND schedule_id IS NOT NULL",
    ):
        assert tools._scan_problem(sql, shared, "a") is None, sql


def test_the_guard_is_honest_when_nothing_is_indexed():
    shared = _huge(indexes=())
    problem = tools._scan_problem("SELECT COUNT(*) FROM plan WHERE project_id = 5", shared, "a")
    assert problem and "no index to filter on" in problem
