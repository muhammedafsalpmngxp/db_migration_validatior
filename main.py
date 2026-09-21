"""CLI runner.

    python main.py
    python main.py "Which tables are unsafe to promote from stage to prod?"
    LLM_PROVIDER=mock python main.py          # no API key needed
"""
import json
import sys

import config
from flow import run_comparison


def main():
    question = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"A = {config.DB_A_LABEL} ({config.DB_A_NAME})")
    print(f"B = {config.DB_B_LABEL} ({config.DB_B_NAME})")
    print(f"LLM provider: {config.LLM_PROVIDER} / {config.LLM_MODEL}\n")

    shared = run_comparison(question, on_progress=lambda m: print(f"  {m}"))

    print("\n" + "=" * 70)
    print(shared["report"])
    print("=" * 70)

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
            },
            fh,
            indent=2,
            default=str,
        )
    print("\nFull run written to last_run.json")


if __name__ == "__main__":
    main()
