"""Single entry point for every LLM call in the project (PocketFlow convention).

Providers:
  openai     - GPT via the official SDK    (needs OPENAI_API_KEY, the project default)
  anthropic  - Claude via the official SDK (needs ANTHROPIC_API_KEY and `uv sync --extra anthropic`)
  mock       - deterministic offline stub, lets the full agent loop run without a key
"""
import os
import re
import time

import config
from utils.observability import LLM_STATS, estimate_tokens, get_logger

log = get_logger("dbcompare.llm")

# ---------------------------------------------------------------- public API


def call_llm(prompt, system=None, label=None):
    """Run one LLM call, logging it and accounting its token usage.

    Every call is counted in ``LLM_STATS`` - successes and failures alike - so the
    UI can report how many calls a run made and what they cost.
    """
    provider = os.getenv("LLM_PROVIDER", config.LLM_PROVIDER).lower()
    model = os.getenv("LLM_MODEL", config.LLM_MODEL)
    label = label or "llm"
    started = time.time()

    log.info("LLM call #%s [%s] provider=%s model=%s prompt_chars=%s",
             LLM_STATS.calls + 1, label, provider, model, len(prompt))
    try:
        if provider == "mock":
            text, usage = _mock_llm(prompt), None
        elif provider == "anthropic":
            text, usage = _anthropic(prompt, system)
        elif provider == "openai":
            text, usage = _openai(prompt, system)
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {provider}")
    except Exception as exc:
        elapsed = time.time() - started
        LLM_STATS.record(provider, model, estimate_tokens(prompt), 0, elapsed,
                         estimated=True, error=exc, label=label, prompt=prompt)
        log.exception("LLM call [%s] failed after %.2fs: %s", label, elapsed, exc)
        raise

    elapsed = time.time() - started
    if usage:
        prompt_tokens, completion_tokens, estimated = usage[0], usage[1], False
        cached = usage[2] if len(usage) > 2 else 0
    else:
        prompt_tokens = estimate_tokens((system or "") + prompt)
        completion_tokens = estimate_tokens(text)
        estimated = True
        cached = 0
    LLM_STATS.record(provider, model, prompt_tokens, completion_tokens, elapsed,
                     estimated=estimated, label=label, prompt=prompt, response=text,
                     cached=cached)
    log.info("LLM call [%s] ok in %.2fs - tokens in=%s (%s cached) out=%s%s",
             label, elapsed, prompt_tokens, cached, completion_tokens,
             " (estimated)" if estimated else "")
    return text


# ---------------------------------------------------------------- providers


def _anthropic(prompt, system):
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    kwargs = dict(
        model=os.getenv("LLM_MODEL", config.LLM_MODEL),
        max_tokens=config.LLM_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = getattr(resp, "usage", None)
    if usage is None:
        return text, None
    return text, (
        getattr(usage, "input_tokens", 0),
        getattr(usage, "output_tokens", 0),
        getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def _openai(prompt, system):
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Put it in .env, export it, or paste it in the "
            "Streamlit sidebar."
        )
    base_url = os.getenv("OPENAI_BASE_URL") or getattr(config, "OPENAI_BASE_URL", None)
    client = OpenAI(api_key=api_key, **({"base_url": base_url} if base_url else {}))

    model = os.getenv("LLM_MODEL", config.LLM_MODEL)
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages, max_tokens=config.LLM_MAX_TOKENS
        )
    except Exception as exc:
        # Reasoning models (o-series, gpt-5) reject max_tokens and want max_completion_tokens.
        if "max_tokens" not in str(exc):
            raise
        log.warning("Retrying with max_completion_tokens: %s", exc)
        resp = client.chat.completions.create(
            model=model, messages=messages, max_completion_tokens=config.LLM_MAX_TOKENS
        )
    text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return text, None
    # Providers cache long prompt prefixes and bill them at a discount; the count is
    # reported here, which is the only way to know the ordering of our prompt is working.
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details else 0
    return text, (
        getattr(usage, "prompt_tokens", 0),
        getattr(usage, "completion_tokens", 0),
        cached or 0,
    )


# ---------------------------------------------------------------- mock


def _mock_llm(prompt):
    """Deterministic replies keyed off the markers the prompts carry.

    Step 0 -> compare_row_counts, step 1/2 -> compare_table_data on the first two
    common tables, then finish. Enough to exercise every branch of the flow.
    """
    if "## UNMATCHED IN A" in prompt:
        return (
            "```yaml\n"
            "thinking: |\n"
            "  Name based matching already covered the obvious ones.\n"
            "pairs: []\n"
            "```"
        )

    if "## REVIEW TASK" in prompt:
        return (
            "```yaml\n"
            "thinking: |\n"
            "  The report cites the structural diff and the row level findings that are\n"
            "  present in the evidence, and invents nothing.\n"
            "approve: true\n"
            "feedback: Grounded in the collected evidence.\n"
            "```"
        )

    if "## WRITE THE FINAL REPORT" in prompt:
        return (
            "# Database comparison report\n\n"
            "## Summary\n"
            "Mock provider output. The structural and row level findings collected by the\n"
            "agent are listed in the evidence section of this run.\n\n"
            "## Schema differences\n"
            "See the structural diff panel for tables and columns that exist on only one side.\n\n"
            "## Data differences\n"
            "See the per table findings gathered during the agent loop.\n\n"
            "## Recommended actions\n"
            "- Review tables that exist on only one side before promoting the schema.\n"
            "- Reconcile rows flagged as missing or modified.\n"
        )

    # Otherwise: a DecideAction prompt.
    steps = 0
    m = re.search(r"OBSERVATIONS SO FAR \((\d+)", prompt)
    if m:
        steps = int(m.group(1))

    common = []
    m = re.search(r"COMMON TABLES: (.*)", prompt)
    if m and m.group(1).strip() not in ("", "none"):
        common = [t.strip() for t in m.group(1).split(",") if t.strip()]

    if steps == 0:
        return (
            "```yaml\n"
            "thinking: |\n"
            "  Start broad: row counts on every shared table show where the data drifted.\n"
            "tool: compare_row_counts\n"
            "reason: Locate tables whose volumes disagree before inspecting rows.\n"
            "params: {}\n"
            "```"
        )
    if steps <= len(common) and steps <= 2:
        table = common[steps - 1]
        return (
            "```yaml\n"
            "thinking: |\n"
            f"  Drill into {table} row by row using the primary key.\n"
            "tool: compare_table_data\n"
            f"reason: Find missing and modified rows in {table}.\n"
            "params:\n"
            f"  table: {table}\n"
            "  limit: 500\n"
            "```"
        )
    return (
        "```yaml\n"
        "thinking: |\n"
        "  Structural diff plus row level checks are enough to write the report.\n"
        "tool: finish\n"
        "reason: Sufficient evidence collected.\n"
        "params: {}\n"
        "```"
    )


# ---------------------------------------------------------------- helpers


def extract_yaml(text):
    """Pull the first ```yaml fenced block out of an LLM reply (PocketFlow convention)."""
    import yaml

    if "```yaml" in text:
        block = text.split("```yaml", 1)[1].split("```", 1)[0]
    elif "```" in text:
        block = text.split("```", 1)[1].split("```", 1)[0]
    else:
        block = text
    parsed = yaml.safe_load(block)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a YAML mapping, got {type(parsed).__name__}")
    return parsed
