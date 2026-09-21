"""Tests.

    LLM_PROVIDER=mock pytest -v test_project.py

Needs the sample databases (run setup_test_data.py first). The DB tests skip themselves
if Postgres is unreachable; the pure diff tests always run.
"""
import os

import pytest

os.environ.setdefault("LLM_PROVIDER", "mock")

import config  # noqa: E402
from utils import db, differ  # noqa: E402


def _postgres_available():
    try:
        db.ping(config.dsn_a())
        db.ping(config.dsn_b())
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(
    not _postgres_available(), reason="sample databases not reachable; run setup_test_data.py"
)


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


# ---------------------------------------------------------------- database


@needs_pg
def test_schema_introspection_matches_the_planted_differences():
    sa = db.fetch_schema(config.dsn_a())
    sb = db.fetch_schema(config.dsn_b())
    d = differ.diff_schemas(sa, sb)

    assert d["tables_only_in_a"] == ["products"]
    assert d["tables_only_in_b"] == ["inventory"]
    assert set(d["common_tables"]) == {"customers", "orders", "order_items"}

    cust = d["table_diffs"]["customers"]
    assert cust["columns_only_in_a"] == ["is_active"]
    assert cust["columns_only_in_b"] == ["loyalty_tier"]
    types = {t["column"]: (t["a"], t["b"]) for t in cust["type_changes"]}
    assert types["email"] == ("character varying(120)", "character varying(255)")
    assert types["country"] == ("character varying(2)", "character varying(3)")

    orders = d["table_diffs"]["orders"]
    assert {t["column"] for t in orders["type_changes"]} == {"amount"}
    assert orders["nullability_changes"][0]["column"] == "status"
    assert orders["indexes_only_in_b"] == ["idx_orders_status"]

    assert d["table_diffs"]["order_items"]["identical"] is True  # schema same, data drifts


@needs_pg
def test_row_level_drift_in_customers_and_orders():
    import tools

    shared = {"schema_diff": differ.diff_schemas(
        db.fetch_schema(config.dsn_a()), db.fetch_schema(config.dsn_b())
    )}

    _, cust = tools.execute_tool("compare_table_data", {"table": "customers"}, shared)
    assert cust["only_in_a"] == [{"customer_id": 8}]
    assert cust["only_in_b"] == [{"customer_id": 9}]
    changed = {m["key"]["customer_id"]: m["changes"] for m in cust["modified"]}
    assert "email" in changed[3] and "country" in changed[5]

    _, orders = tools.execute_tool("compare_table_data", {"table": "orders"}, shared)
    assert orders["only_in_a"] == [{"order_id": 12}]
    assert orders["only_in_b"] == [{"order_id": 13}]
    changed = {m["key"]["order_id"]: m["changes"] for m in orders["modified"]}
    assert changed[7]["amount"] == {"a": 199.99, "b": 189.99}
    assert changed[10]["status"] == {"a": "shipped", "b": "delivered"}


@needs_pg
def test_run_sql_rejects_writes_and_multiple_statements():
    with pytest.raises(ValueError):
        db.run_select(config.dsn_a(), "DELETE FROM customers")
    with pytest.raises(ValueError):
        db.run_select(config.dsn_a(), "SELECT 1; DROP TABLE customers")
    rows = db.run_select(config.dsn_a(), "SELECT count(*) AS n FROM customers")
    assert rows[0]["n"] == 8


@needs_pg
def test_full_flow_end_to_end_with_mock_llm():
    from flow import run_comparison

    shared = run_comparison("What changed between the two databases?")

    assert shared["schema_diff"]["tables_only_in_a"] == ["products"]
    assert shared["history"], "the agent should have called at least one tool"
    assert shared["history"][0]["tool"] == "compare_row_counts"
    assert any(h["tool"] == "compare_table_data" for h in shared["history"])
    assert shared["report"].startswith("#")
    assert shared["reviews"][-1]["approve"] is True
    assert "customers" in shared["data_diffs"]


@needs_pg
def test_agent_recovers_from_a_bad_tool_call():
    """A tool error becomes an observation instead of killing the run."""
    import tools
    from nodes import ExecuteToolNode

    shared = {"schema_diff": {"common_tables": ["customers"], "table_diffs": {}}, "history": []}
    shared["next_action"] = {
        "tool": "compare_table_data",
        "params": {"table": "does_not_exist"},
        "thinking": "",
        "reason": "probe",
    }
    ExecuteToolNode().run(shared)
    assert "TOOL ERROR" in shared["history"][0]["observation"]
    assert tools.VALID_TOOLS  # sanity


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
