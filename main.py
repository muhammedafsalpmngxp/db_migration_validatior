"""CLI runner.

    python main.py
    python main.py "Which tables are unsafe to promote from stage to prod?"
    LLM_PROVIDER=mock python main.py          # no API key needed
"""
import json
import sys

import config
from flow import run_comparison
from utils.observability import LOG_FILE, setup_logging


def main():
    setup_logging()
    question = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"A = {config.DB_A_LABEL} ({config.DB_A_NAME})")
    print(f"B = {config.DB_B_LABEL} ({config.DB_B_NAME})")
    print(f"LLM provider: {config.LLM_PROVIDER} / {config.LLM_MODEL}\n")

    shared = run_comparison(question, on_progress=lambda m: print(f"  {m}"))

    print("\n" + "=" * 70)
    print(shared["report"])
    print("=" * 70)

    stats = shared.get("llm_stats", {})
    print(
        f"\nLLM calls: {stats.get('calls', 0)}"
        f" ({stats.get('failures', 0)} failed)"
        f" | tokens: {stats.get('total_tokens', 0)}"
        f" (in {stats.get('prompt_tokens', 0)} / out {stats.get('completion_tokens', 0)}"
        f", {stats.get('cached_tokens', 0)} cached)"
        f"{' estimated' if stats.get('estimated') else ''}"
        f" | LLM time: {stats.get('seconds', 0)}s | run time: {shared.get('seconds', 0)}s"
    )
    errors = shared.get("errors", [])
    if errors:
        print(f"\n{len(errors)} error(s) during the run:")
        for e in errors:
            print(f"  [{e['where']}] {e['type']}: {e['message']}")
    print(f"Log file: {LOG_FILE}")

    with open("last_run.json", "w") as fh:
        json.dump(
            {
                "question": shared["question"],
                "schema_diff": shared.get("schema_diff"),
                "data_diffs": shared.get("data_diffs"),
                "history": [
                    {k: v for k, v in h.items() if k != "raw"} for h in shared.get("history", [])
                ],
                "reviews": shared.get("reviews"),
                "report": shared.get("report"),
                "llm_stats": shared.get("llm_stats"),
                "errors": shared.get("errors"),
                "seconds": shared.get("seconds"),
                "logs": shared.get("logs"),
            },
            fh,
            indent=2,
            default=str,
        )
    print("\nFull run written to last_run.json")


if __name__ == "__main__":
    main()
