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
from utils.observability import get_logger

logger = get_logger("dbcompare.nodes")


# An observation is re-sent on every later call, so its cost is multiplied by the steps
# that follow it. The head carries the counts and the first examples, which is what the
# agent reasons about; the full text stays in the shared store for the UI and the log.
OBSERVATION_CHARS = 1200
REPORT_OBSERVATION_CHARS = 2500


def _clip(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n  ... [{len(text) - limit} more characters, see Agent steps]"


def log(shared, message):
    """Append to the run trace, write it to the log file and push it to the UI callback."""
    shared.setdefault("trace", []).append(message)
    logger.info(message)
    cb = shared.get("on_progress")
    if callable(cb):
        cb(message)


def log_error(shared, where, exc, recovered=False):
    """Record a swallowed exception so the run can still show what went wrong.

    `recovered` separates the two kinds: a tool call that failed and became an
    observation the agent worked around, versus something that actually cost the run.
    Both belong in the log; only the second is a problem to go and look at.
    """
    logger.error("%s failed: %s", where, exc, exc_info=exc)
    shared.setdefault("errors", []).append(
        {
            "where": where,
            "type": type(exc).__name__,
            "message": str(exc),
            "recovered": recovered,
        }
    )


class ErrorTracking:
    """Lets a node stash the exception its fallback swallowed and move it into `shared`.

    `exec_fallback` never sees the shared store, so the exception is parked on the node
    and drained in `post`, where the store is available.
    """

    last_error = None

    #: a failure this node answers for itself, rather than one that costs the run
    recovers = False

    def _drain_error(self, shared):
        if self.last_error:
            where, exc = self.last_error
            self.last_error = None
            log_error(shared, where, exc, recovered=self.recovers)


# ---------------------------------------------------------------- 1. introspect


class FetchSchemasNode(BatchNode):
    """Reads both schemas. BatchNode so each database is one independent unit of work."""

    def prep(self, shared):
        chosen = shared.get("tables")
        scope = f"{len(chosen)} selected table(s)" if chosen else "every table"
        log(shared, f"Reading schemas from {config.DB_A_NAME} and {config.DB_B_NAME} ({scope})")
        return [("a", config.dsn_a(), chosen), ("b", config.dsn_b(), chosen)]

    def exec(self, item):
        from utils import db

        side, dsn, chosen = item
        return side, db.fetch_schema(dsn, config.SCHEMA, only=chosen)

    def exec_fallback(self, prep_res, exc):
        logger.exception("Schema introspection failed: %s", exc)
        raise RuntimeError(f"Could not read schema: {exc}") from exc

    def post(self, shared, prep_res, exec_res):
        # Filtering happens in the query (see fetch_schema(only=...)). The raw, schema
        # qualified schemas are kept as they are; MatchTablesNode decides what pairs
        # with what, because that is a judgement call, not introspection.
        shared["raw_schema"] = {side: schema for side, schema in exec_res}
        for side, schema in exec_res:
            schemas = sorted({e["schema"] for e in schema.values()})
            log(shared, f"  {side.upper()}: {len(schema)} tables in {', '.join(schemas) or '-'}")
        return "default"


MATCH_PROMPT = """You are matching the tables of two Microsoft SQL Server databases that
grew apart. Same data, renamed and moved between schemas.

A = {label_a}
B = {label_b}

Tables already matched automatically (same name, or same name ignoring case and
underscores) - do not repeat these:
{already}

## UNMATCHED IN A
{unmatched_a}

## UNMATCHED IN B
{unmatched_b}

Pair up the ones that clearly hold the same thing. Judge by the name only: a rename
(`WMR` -> `well_master_report`), an abbreviation, a plural, a prefix or suffix that is
obviously noise (`_V2`, `_new`, `Staging_`, `_backup`).

Rules:
- Use the full `schema.table` names exactly as written above, and each table at most once.
- Only pair what you would defend to a colleague. Leave the rest unmatched; an unmatched
  table is a real answer and far better than a wrong pair.
- A backup, staging or history copy is NOT the same table as the live one.

Reply with a YAML block and nothing else:

```yaml
thinking: |
  <what you matched on, a few lines>
pairs:
  - a: <schema.table in A>
    b: <schema.table in B>
    why: <a few words>
```
If nothing matches, return an empty list."""


class MatchTablesNode(ErrorTracking, Node):
    """Decides which table on A is which table on B.

    Exact and normalised names are matched deterministically; whatever is left over is
    put to the LLM, which is the part that needs judgement (`WMR` vs `well_master_report`).
    Every proposal is checked against the leftover lists before it is believed, so the
    model can suggest a pair but never invent a table.
    """

    def __init__(self, **kwargs):
        super().__init__(max_retries=kwargs.pop("max_retries", 2), wait=kwargs.pop("wait", 2))

    def prep(self, shared):
        raw = shared["raw_schema"]
        picked = set(shared.get("tables") or [])
        pairs, unmatched_a, unmatched_b = differ.match_tables(
            raw["a"], raw["b"],
            chosen_a=picked & set(raw["a"]),
            chosen_b=picked & set(raw["b"]),
        )
        log(shared, f"Table matching: {len(pairs)} by name, "
                    f"{len(unmatched_a)} unmatched in A, {len(unmatched_b)} in B")
        return {
            "pairs": pairs,
            "unmatched_a": unmatched_a,
            "unmatched_b": unmatched_b,
            # The agent is only asked when the user did not say which tables to compare.
            "ask_llm": bool(unmatched_a and unmatched_b and not picked),
            "label_a": config.DB_A_LABEL,
            "label_b": config.DB_B_LABEL,
        }

    def exec(self, ctx):
        if not ctx["ask_llm"]:
            return []
        reply = call_llm(
            MATCH_PROMPT.format(
                label_a=ctx["label_a"],
                label_b=ctx["label_b"],
                already=", ".join(p["key"] for p in ctx["pairs"]) or "none",
                unmatched_a="\n".join(f"- {t}" for t in ctx["unmatched_a"]),
                unmatched_b="\n".join(f"- {t}" for t in ctx["unmatched_b"]),
            ),
            label="match tables",
        )
        proposed = extract_yaml(reply).get("pairs") or []
        if not isinstance(proposed, list):
            raise ValueError("pairs must be a list")

        # Believe nothing that is not still on both leftover lists.
        left_a, left_b = set(ctx["unmatched_a"]), set(ctx["unmatched_b"])
        accepted = []
        for item in proposed:
            if not isinstance(item, dict):
                continue
            qa, qb = str(item.get("a", "")).strip(), str(item.get("b", "")).strip()
            if qa in left_a and qb in left_b:
                left_a.discard(qa)
                left_b.discard(qb)
                accepted.append(
                    {"a": qa, "b": qb, "method": "agent", "why": str(item.get("why", "")).strip()}
                )
        return accepted

    def exec_fallback(self, prep_res, exc):
        # A failed match is not a failed run: carry on with the names that matched.
        logger.exception("Table matching by LLM failed: %s", exc)
        self.last_error = ("MatchTablesNode", exc)
        return []

    def post(self, shared, prep_res, exec_res):
        self._drain_error(shared)
        raw = shared["raw_schema"]
        picked = set(shared.get("tables") or [])
        pairs, unmatched_a, unmatched_b = differ.match_tables(
            raw["a"], raw["b"],
            chosen_a=picked & set(raw["a"]),
            chosen_b=picked & set(raw["b"]),
            extra_pairs=exec_res,
        )
        by_method = {}
        for p in pairs:
            by_method[p["method"]] = by_method.get(p["method"], 0) + 1

        shared["table_matches"] = {
            "pairs": pairs,
            "unmatched_a": unmatched_a,
            "unmatched_b": unmatched_b,
            "by_method": by_method,
        }
        shared["schema"] = dict(
            zip(("a", "b"), differ.apply_matches(raw["a"], raw["b"], pairs))
        )

        if exec_res:
            log(shared, f"  agent matched {len(exec_res)} more: "
                        + ", ".join(f"{p['a']} <-> {p['b']}" for p in exec_res[:6])
                        + (" ..." if len(exec_res) > 6 else ""))
        moved = [
            f"{p['a']} <-> {p['b']}"
            for p in pairs
            if raw["a"][p["a"]]["schema"] != raw["b"][p["b"]]["schema"]
        ]
        if moved:
            log(shared, f"  paired across schemas: {', '.join(moved[:6])}"
                        + (" ..." if len(moved) > 6 else ""))
        log(shared, f"  matched {len(pairs)} table(s) ({by_method}); "
                    f"{len(unmatched_a)} only in A, {len(unmatched_b)} only in B")
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


# The order of this prompt is deliberate. Providers cache prompt prefixes (OpenAI does it
# automatically above ~1,024 tokens, at about half price), and a cache only helps while
# the prefix is byte for byte identical. Everything fixed for the whole run - the role,
# the tool catalog, the rules, the question, the structural diff - therefore comes first,
# and the part that grows with every step comes last. Eight of a run's calls share that
# prefix.
DECIDE_PROMPT = """You are a database comparison agent. Two Microsoft SQL Server databases are being
compared and you decide, one step at a time, which investigation to run next.

## DATABASES
A = {label_a} (database {db_a})
B = {label_b} (database {db_b})

## TOOL CATALOG
{catalog}

## HOW TO CHOOSE
- Never repeat a tool call you have already run with the same params: the result will be
  the same. If a call failed, change the call - a different table, different SQL - or
  move on.
- A table marked ANSWERED is finished: the diff above already holds everything a tool
  could tell you about it, so calling one is a wasted step. Move to another table.
- Prefer compare_table_data on tables whose row counts differ or whose schema changed,
  and that show a key in the list above.
- Steps are scarce. If no table is left that a tool can add to, choose finish - a short
  investigation with a complete answer beats spending the budget to say the same thing.
- Answer the user question specifically; do not wander.
- Choose finish as soon as you can answer it, and always by step {max_steps}.

## USER QUESTION
{question}

## STRUCTURAL DIFF (already computed, trustworthy)
{schema_summary}

COMMON TABLES: {common_tables}

## OBSERVATIONS SO FAR ({n_steps} steps, budget {max_steps})
{history}
{feedback}
Reply with a YAML block and nothing else:

```yaml
thinking: |
  <your reasoning, a few lines>
tool: <one tool name from the catalog>
reason: <one line on why this call, now>
params:
  <key>: <value>
```"""


class DecideActionNode(ErrorTracking, Node):
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
                f"{_clip(h['observation'], OBSERVATION_CHARS)}"
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
                diff, config.DB_A_LABEL, config.DB_B_LABEL,
                row_compare_max=config.ROW_COMPARE_MAX_ROWS,
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
        reply = call_llm(DECIDE_PROMPT.format(**ctx), label="decide")
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
        logger.exception("DecideAction gave up after retries: %s", exc)
        self.last_error = ("DecideActionNode", exc)
        return {"tool": "finish", "params": {}, "thinking": "", "reason": f"LLM error: {exc}"}

    def post(self, shared, prep_res, exec_res):
        self._drain_error(shared)
        shared["next_action"] = exec_res
        if exec_res["tool"] == "finish" or len(shared.get("history", [])) >= config.MAX_AGENT_STEPS:
            log(shared, f"Agent: finished investigating. {exec_res['reason']}")
            return "report"
        log(shared, f"Agent: {exec_res['tool']} - {exec_res['reason']}")
        return "tool"


def _already_run(shared, action):
    """The earlier step that made this exact call, or None.

    Params are compared as sorted JSON so that {"table": "x", "limit": 5000} and
    {"limit": 5000, "table": "x"} count as the same call.
    """
    signature = (action["tool"], json.dumps(action.get("params") or {}, sort_keys=True))
    for i, h in enumerate(shared.get("history", []), 1):
        if (h["tool"], json.dumps(h.get("params") or {}, sort_keys=True)) == signature:
            return {"step": i, "observation": h["observation"], "raw": h.get("raw")}
    return None


class ExecuteToolNode(ErrorTracking, Node):
    """The 'act' half: runs the chosen tool and feeds the observation back into the loop."""

    recovers = True  # a failed tool call is an observation, not a broken run

    def prep(self, shared):
        return shared["next_action"], shared

    def exec(self, inputs):
        action, shared = inputs

        # The same call gives the same answer, so running it again buys nothing and costs
        # a step and a full prompt. The agent is told not to repeat itself, but a model
        # that has just seen an error it does not understand will try the identical call
        # again - three times, in one measured run. Answering from the history stops that
        # without pretending the call succeeded.
        previous = _already_run(shared, action)
        if previous is not None:
            return {
                "observation": (
                    f"You already ran this exact call at step {previous['step']}. The"
                    f" result has not changed:\n{previous['observation']}\n"
                    f"Change the call or choose another tool."
                ),
                "raw": previous["raw"],
                "seconds": 0.0,
                "repeated": True,
            }

        started = time.time()
        summary, raw = tools.execute_tool(action["tool"], action["params"], shared)
        return {"observation": summary, "raw": raw, "seconds": round(time.time() - started, 2)}

    def exec_fallback(self, prep_res, exc):
        # A failed tool call is an observation too; the agent can pick another approach.
        logger.exception("Tool call failed: %s", exc)
        self.last_error = ("ExecuteToolNode", exc)
        return {"observation": f"TOOL ERROR: {exc}", "raw": {"error": str(exc)}, "seconds": 0}

    def post(self, shared, prep_res, exec_res):
        self._drain_error(shared)
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
                "repeated": exec_res.get("repeated", False),
            }
        )
        if exec_res.get("repeated"):
            log(shared, "  repeat of an earlier call, answered from history")
        else:
            log(shared, f"  done in {exec_res['seconds']}s")
        return "decide"


# ---------------------------------------------------------------- 3. report


REPORT_PROMPT = """## WRITE THE FINAL REPORT

You are reporting the differences between two Microsoft SQL Server databases to an engineer who has
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


class ComposeReportNode(ErrorTracking, Node):
    def __init__(self, **kwargs):
        super().__init__(max_retries=kwargs.pop("max_retries", 2), wait=kwargs.pop("wait", 2))

    def prep(self, shared):
        diff = shared.get("schema_diff", {})
        history = "\n\n".join(
            f"### {h['tool']} {json.dumps(h.get('params') or {})}\n"
            f"{_clip(h['observation'], REPORT_OBSERVATION_CHARS)}"
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
        return call_llm(REPORT_PROMPT.format(**ctx), label="report").strip()

    def exec_fallback(self, prep_res, exc):
        logger.exception("Report generation failed: %s", exc)
        self.last_error = ("ComposeReportNode", exc)
        return (
            "# Database comparison report\n\n"
            f"The report could not be generated ({exc}). The structural diff below is still "
            "valid because it is computed, not generated.\n\n```\n"
            + prep_res["schema_summary"]
            + "\n```"
        )

    def post(self, shared, prep_res, exec_res):
        self._drain_error(shared)
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


class SupervisorNode(ErrorTracking, Node):
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
            "history": "\n\n".join(
                _clip(h["observation"], REPORT_OBSERVATION_CHARS)
                for h in shared.get("history", [])
            ),
            "report": shared.get("report", ""),
        }

    def exec(self, ctx):
        reply = call_llm(SUPERVISE_PROMPT.format(**ctx), label="supervise")
        verdict = extract_yaml(reply)
        return {
            "approve": bool(verdict.get("approve")),
            "feedback": str(verdict.get("feedback", "")).strip(),
        }

    def exec_fallback(self, prep_res, exc):
        logger.exception("Supervisor review failed: %s", exc)
        self.last_error = ("SupervisorNode", exc)
        return {"approve": True, "feedback": f"Review skipped: {exc}"}

    def post(self, shared, prep_res, exec_res):
        self._drain_error(shared)
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
