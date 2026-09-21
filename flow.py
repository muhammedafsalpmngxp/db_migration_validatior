"""Flow definition: nodes wired into the comparison graph."""
from pocketflow import Flow

from nodes import (
    ComposeReportNode,
    DecideActionNode,
    DoneNode,
    ExecuteToolNode,
    FetchSchemasNode,
    SchemaDiffNode,
    SupervisorNode,
)


def create_comparison_flow():
    fetch = FetchSchemasNode()
    schema_diff = SchemaDiffNode()
    decide = DecideActionNode()
    execute = ExecuteToolNode()
    report = ComposeReportNode()
    supervisor = SupervisorNode()
    done = DoneNode()

    fetch >> schema_diff >> decide

    decide - "tool" >> execute          # think -> act
    execute - "decide" >> decide        # act -> think (the agent loop)
    decide - "report" >> report         # enough evidence

    report - "supervise" >> supervisor
    supervisor - "retry" >> decide      # send the agent back for more evidence
    supervisor - "approve" >> done

    return Flow(start=fetch)


def run_comparison(question=None, on_progress=None):
    """Convenience wrapper: build the shared store, run the flow, return it."""
    shared = {
        "question": question or "Compare the two databases end to end and tell me what differs.",
        "on_progress": on_progress,
        "history": [],
        "trace": [],
    }
    create_comparison_flow().run(shared)
    return shared
