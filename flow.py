"""Flow definition: nodes wired into the comparison graph."""
import time
import traceback

from pocketflow import Flow

from nodes import (
    ComposeReportNode,
    DecideActionNode,
    DoneNode,
    ExecuteToolNode,
    FetchSchemasNode,
    MatchTablesNode,
    SchemaDiffNode,
    SupervisorNode,
)
from utils.observability import BUFFER as LOG_BUFFER, LLM_STATS, get_logger, setup_logging

logger = get_logger("dbcompare.flow")

# The most recent shared store, kept so a caller can still inspect logs, errors and LLM
# usage after run_comparison() raised.
LAST_RUN = {}


def create_comparison_flow():
    fetch = FetchSchemasNode()
    match = MatchTablesNode()
    schema_diff = SchemaDiffNode()
    decide = DecideActionNode()
    execute = ExecuteToolNode()
    report = ComposeReportNode()
    supervisor = SupervisorNode()
    done = DoneNode()

    fetch >> match >> schema_diff >> decide

    decide - "tool" >> execute          # think -> act
    execute - "decide" >> decide        # act -> think (the agent loop)
    decide - "report" >> report         # enough evidence

    report - "supervise" >> supervisor
    supervisor - "retry" >> decide      # send the agent back for more evidence
    supervisor - "approve" >> done

    return Flow(start=fetch)


def run_comparison(question=None, on_progress=None, tables=None):
    """Build the shared store, run the flow, return it.

    `tables` narrows the whole run to those table names; None compares everything.
    The store always comes back - even when the run blows up - carrying `errors`,
    `llm_stats` and `logs` so the caller can show what happened.
    """
    setup_logging()
    LLM_STATS.reset()
    LOG_BUFFER.clear()

    shared = {
        "question": question or "Compare the two databases end to end and tell me what differs.",
        "on_progress": on_progress,
        "tables": list(tables) if tables else None,
        "history": [],
        "trace": [],
        "errors": [],
    }
    global LAST_RUN
    LAST_RUN = shared
    started = time.time()
    logger.info("=== run start: %s", shared["question"])
    try:
        create_comparison_flow().run(shared)
        shared["failed"] = False
    except Exception as exc:
        logger.error("Run aborted: %s", exc, exc_info=exc)
        shared["failed"] = True
        shared["errors"].append(
            {
                "where": "flow",
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        raise
    finally:
        shared["seconds"] = round(time.time() - started, 2)
        shared["llm_stats"] = LLM_STATS.summary()
        shared["logs"] = LOG_BUFFER.snapshot()
        stats = shared["llm_stats"]
        logger.info(
            "=== run end in %ss - %s LLM calls, %s tokens (in %s / out %s), %s failures, "
            "%s errors",
            shared["seconds"], stats["calls"], stats["total_tokens"],
            stats["prompt_tokens"], stats["completion_tokens"], stats["failures"],
            len(shared["errors"]),
        )
    return shared
