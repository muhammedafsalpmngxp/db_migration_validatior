"""PocketFlow nodes.

Shape of the graph (see flow.py):

    FetchSchemas -> SchemaDiff -> Decide --tool--> ExecuteTool --+
                                    ^                            |
                                    +----------------------------+
                                    |
                                    +--report--> ComposeReport -> Supervisor --approve--> Done
                                    ^                                  |
                                    +---------------retry--------------+
"""
import json
import time

from pocketflow import BatchNode, Node

import config
import tools
from utils import differ
from utils.call_llm import call_llm, extract_yaml


def log(shared, message):
    """Append to the run trace and push it to the UI callback if one is registered."""
    shared.setdefault("trace", []).append(message)
    cb = shared.get("on_progress")
    if callable(cb):
        cb(message)


# ---------------------------------------------------------------- 1. introspect


class FetchSchemasNode(BatchNode):
    """Reads both schemas. BatchNode so each database is one independent unit of work."""

    def prep(self, shared):
        log(shared, f"Reading schemas from {config.DB_A_NAME} and {config.DB_B_NAME}")
        return [("a", config.dsn_a()), ("b", config.dsn_b())]

    def exec(self, item):
        from utils import db

        side, dsn = item
        return side, db.fetch_schema(dsn, config.SCHEMA)

    def exec_fallback(self, prep_res, exc):
        raise RuntimeError(f"Could not read schema: {exc}") from exc

    def post(self, shared, prep_res, exec_res):
        shared["schema"] = {side: schema for side, schema in exec_res}
        for side, schema in exec_res:
            log(shared, f"  {side.upper()}: {len(schema)} tables")
        return "default"


class SchemaDiffNode(Node):
    """Deterministic structural diff. No LLM: the facts are computed, not generated."""

    def prep(self, shared):
        return shared["schema"]["a"], shared["schema"]["b"]

    def exec(self, inputs):
        a, b = inputs
        return differ.diff_schemas(a, b)

    def post(self, shared, prep_res, exec_res):
        shared["schema_diff"] = exec_res
        log(
            shared,
            "Structural diff: "
            f"{len(exec_res['tables_only_in_a'])} tables only in A, "
            f"{len(exec_res['tables_only_in_b'])} only in B, "
            f"{len(exec_res['tables_with_schema_changes'])} changed, "
            f"{len(exec_res['row_count_mismatches'])} row count mismatches",
        )
        return "default"


# ---------------------------------------------------------------- 2. agent loop


DECIDE_PROMPT = """You are a database comparison agent. Two Postgres databases are being
compared and you decide, one step at a time, which investigation to run next.

## USER QUESTION
{question}

## DATABASES
A = {label_a} (database {db_a})
B = {label_b} (database {db_b})

## STRUCTURAL DIFF (already computed, trustworthy)
{schema_summary}

COMMON TABLES: {common_tables}

## OBSERVATIONS SO FAR ({n_steps} steps, budget {max_steps})
{history}
{feedback}
## TOOL CATALOG
{catalog}

## HOW TO CHOOSE
- Do not repeat a tool call you already ran with the same params.
- Prefer compare_table_data on tables whose row counts differ or whose schema changed.
- Answer the user question specifically; do not wander.
- Choose finish as soon as you can answer it, and always by step {max_steps}.

Reply with a YAML block and nothing else:

```yaml
thinking: |
  <your reasoning, a few lines>
tool: <one tool name from the catalog>
reason: <one line on why this call, now>
params:
  <key>: <value>
```"""


class DecideActionNode(Node):
    """The 'think' half of the agent loop (Agentic RAG / supervisor cookbook pattern)."""

    def __init__(self, **kwargs):
        super().__init__(max_retries=kwargs.pop("max_retries", 3), wait=kwargs.pop("wait", 2))

    def prep(self, shared):
        diff = shared.get("schema_diff", {})
        history = shared.setdefault("history", [])
        blocks = []
        for i, h in enumerate(history, 1):
            blocks.append(
                f"### Step {i}: {h['tool']} {json.dumps(h.get('params') or {})}\n"
                f"{h['observation']}"
            )
        feedback = ""
        if shared.get("supervisor_feedback"):
            feedback = (
                "\n## REVIEWER FEEDBACK ON YOUR PREVIOUS REPORT\n"
                f"{shared['supervisor_feedback']}\nGather what is missing, then finish.\n"
            )
        return {
            "question": shared.get("question") or "Compare the two databases end to end.",
            "label_a": config.DB_A_LABEL,
            "label_b": config.DB_B_LABEL,
            "db_a": config.DB_A_NAME,
            "db_b": config.DB_B_NAME,
            "schema_summary": differ.summarize_schema_diff(
                diff, config.DB_A_LABEL, config.DB_B_LABEL
            )
            if diff
            else "not computed yet",
            "common_tables": ", ".join(diff.get("common_tables", [])) or "none",
            "n_steps": len(history),
            "max_steps": config.MAX_AGENT_STEPS,
            "history": "\n\n".join(blocks) or "none yet",
            "feedback": feedback,
            "catalog": tools.TOOL_CATALOG,
        }

    def exec(self, ctx):
        reply = call_llm(DECIDE_PROMPT.format(**ctx))
        decision = extract_yaml(reply)
        tool = str(decision.get("tool", "")).strip()
        if tool not in tools.VALID_TOOLS:
            raise ValueError(f"Model chose an unknown tool: {tool!r}")
        params = decision.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be a mapping")
        return {
            "tool": tool,
            "params": params,
            "thinking": str(decision.get("thinking", "")).strip(),
            "reason": str(decision.get("reason", "")).strip(),
        }

    def exec_fallback(self, prep_res, exc):
        # Never let a malformed reply kill the run: stop investigating and report.
        return {"tool": "finish", "params": {}, "thinking": "", "reason": f"LLM error: {exc}"}

    def post(self, shared, prep_res, exec_res):
        shared["next_action"] = exec_res
        if exec_res["tool"] == "finish" or len(shared.get("history", [])) >= config.MAX_AGENT_STEPS:
            log(shared, f"Agent: finished investigating. {exec_res['reason']}")
            return "report"
        log(shared, f"Agent: {exec_res['tool']} - {exec_res['reason']}")
        return "tool"


class ExecuteToolNode(Node):
    """The 'act' half: runs the chosen tool and feeds the observation back into the loop."""

    def prep(self, shared):
        return shared["next_action"], shared

    def exec(self, inputs):
        action, shared = inputs
        started = time.time()
        summary, raw = tools.execute_tool(action["tool"], action["params"], shared)
        return {"observation": summary, "raw": raw, "seconds": round(time.time() - started, 2)}

    def exec_fallback(self, prep_res, exc):
        # A failed tool call is an observation too; the agent can pick another approach.
        return {"observation": f"TOOL ERROR: {exc}", "raw": {"error": str(exc)}, "seconds": 0}

    def post(self, shared, prep_res, exec_res):
        action = prep_res[0]
        shared.setdefault("history", []).append(
            {
                "tool": action["tool"],
                "params": action["params"],
                "thinking": action["thinking"],
                "reason": action["reason"],
                "observation": exec_res["observation"],
                "raw": exec_res["raw"],
                "seconds": exec_res["seconds"],
            }
        )
        log(shared, f"  done in {exec_res['seconds']}s")
        return "decide"


# ---------------------------------------------------------------- 3. report


REPORT_PROMPT = """## WRITE THE FINAL REPORT

You are reporting the differences between two Postgres databases to an engineer who has
to decide whether it is safe to promote B over A.

USER QUESTION: {question}
A = {label_a}   B = {label_b}

## STRUCTURAL DIFF
{schema_summary}

## EVIDENCE GATHERED BY THE AGENT
{history}

Write Markdown with these sections:
# Database comparison report
## Summary            - three or four sentences, lead with the riskiest finding
## Schema differences - a Markdown table: Table | Difference | Impact
## Data differences   - per table, the missing and modified rows, with the key values
## Risk assessment    - what breaks if B replaces A, ordered by severity
## Recommended actions - concrete, numbered

Rules: use only facts that appear above, never invent a table, column or count, and say
plainly when something was not checked. No preamble, start with the heading."""


class ComposeReportNode(Node):
    def __init__(self, **kwargs):
        super().__init__(max_retries=kwargs.pop("max_retries", 2), wait=kwargs.pop("wait", 2))

    def prep(self, shared):
        diff = shared.get("schema_diff", {})
        history = "\n\n".join(
            f"### {h['tool']} {json.dumps(h.get('params') or {})}\n{h['observation']}"
            for h in shared.get("history", [])
        )
        return {
            "question": shared.get("question") or "Compare the two databases end to end.",
            "label_a": config.DB_A_LABEL,
            "label_b": config.DB_B_LABEL,
            "schema_summary": differ.summarize_schema_diff(
                diff, config.DB_A_LABEL, config.DB_B_LABEL
            ),
            "history": history or "no tool calls were made",
        }

    def exec(self, ctx):
        return call_llm(REPORT_PROMPT.format(**ctx)).strip()

    def exec_fallback(self, prep_res, exc):
        return (
            "# Database comparison report\n\n"
            f"The report could not be generated ({exc}). The structural diff below is still "
            "valid because it is computed, not generated.\n\n```\n"
            + prep_res["schema_summary"]
            + "\n```"
        )

    def post(self, shared, prep_res, exec_res):
        shared["report"] = exec_res
        log(shared, "Report drafted, handing it to the supervisor")
        return "supervise"


# ---------------------------------------------------------------- 4. supervisor


SUPERVISE_PROMPT = """## REVIEW TASK

You are the supervisor. Check a draft comparison report against the evidence it was
written from, the way a reviewer would. Reject it only for a real problem:
a claim not supported by the evidence, an invented table/column/number, a finding from
the evidence that is missing from the report, or an empty or malformed report.

## EVIDENCE
{schema_summary}

{history}

## DRAFT REPORT
{report}

Reply with a YAML block and nothing else:

```yaml
thinking: |
  <what you checked>
approve: true or false
feedback: <if false, exactly what to fix or gather next; if true, one line>
```"""


class SupervisorNode(Node):
    """Quality gate from the pocketflow-supervisor cookbook: an unreliable generator
    paired with a cheap reviewer that can send the work back."""

    def __init__(self, **kwargs):
        super().__init__(max_retries=kwargs.pop("max_retries", 2), wait=kwargs.pop("wait", 1))

    def prep(self, shared):
        diff = shared.get("schema_diff", {})
        return {
            "schema_summary": differ.summarize_schema_diff(
                diff, config.DB_A_LABEL, config.DB_B_LABEL
            ),
            "history": "\n\n".join(h["observation"] for h in shared.get("history", [])),
            "report": shared.get("report", ""),
        }

    def exec(self, ctx):
        reply = call_llm(SUPERVISE_PROMPT.format(**ctx))
        verdict = extract_yaml(reply)
        return {
            "approve": bool(verdict.get("approve")),
            "feedback": str(verdict.get("feedback", "")).strip(),
        }

    def exec_fallback(self, prep_res, exc):
        return {"approve": True, "feedback": f"Review skipped: {exc}"}

    def post(self, shared, prep_res, exec_res):
        shared.setdefault("reviews", []).append(exec_res)
        if exec_res["approve"]:
            shared["supervisor_feedback"] = None
            log(shared, "Supervisor approved the report")
            return "approve"
        attempts = shared.get("review_attempts", 0) + 1
        shared["review_attempts"] = attempts
        if attempts > config.MAX_SUPERVISOR_RETRIES:
            log(shared, "Supervisor still unhappy, returning the last draft anyway")
            return "approve"
        shared["supervisor_feedback"] = exec_res["feedback"]
        log(shared, f"Supervisor rejected the draft: {exec_res['feedback']}")
        return "retry"


class DoneNode(Node):
    def post(self, shared, prep_res, exec_res):
        log(shared, "Run complete")
        return None
