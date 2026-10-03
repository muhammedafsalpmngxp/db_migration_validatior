"""The written parts of the report: an executive summary, one summary per part, the top
risks and the next steps.

The AI only puts into words what the checks measured. It gets a fact sheet - counts,
database, table and column names, results and findings, never a row value or a server
address - and answers in a fixed JSON shape. Every number and every table or column name
in its answer must appear in the facts; an answer that uses anything else is sent back once
to be rewritten, and replaced by plain sentences built from the same facts if it still does.
The result (Must fix / Needs a decision / ...) is never taken from the AI.

Answers are kept by the hash of their facts, so rebuilding the files of a run calls no AI.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from .. import ai, config
from . import plain, settings

CACHE_FILE = settings.REPORT_DIR / "ai_cache.json"
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
COMMON = {"e.g", "i.e", "etc", "vs"}          # abbreviations, not names
NAME = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+\b")    # schema.table, snake_case, Pascal_Case

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["executive_summary", "part1_summary", "part2_summary", "top_risks", "next_steps"],
    "properties": {
        "executive_summary": {"type": "string"},
        "part1_summary": {"type": "string"},
        "part2_summary": {"type": "string"},
        "top_risks": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = f"""You write the summary of a database migration check report for managers who have never
worked on a migration. Use plain, calm, short sentences. No technical jargon (no SQL, no "rows"
without saying "records"); say "records" for rows and "information" or "column" for columns.

The migration has two parts; the report calls them Section 1 and Section 2 (never "Part"):
- Section 1: the client's databases on the ATNM server were copied to the RDS server. This must be an
  exact copy.
- Section 2: the copies on RDS were moved into the new system database ({plain.TARGET_DB}): tables renamed,
  reshaped and linked, following the migration plan.

You get a FACT SHEET. Rules:
1. Use only the facts. Never invent a number, table, column or cause. Every number you write must
   appear in the facts.
2. The results are decided already (Must fix / Needs a decision / Not checked / Correct). Never
   change them, never call something fine that the facts call Must fix.
3. Name tables and databases exactly as written in the facts.
4. A cause you suggest must be worded as likely ("likely", "probably"), never as certain.
5. executive_summary: 3 to 5 sentences: the overall result, the biggest problems, what it means.
   part1_summary and part2_summary: 2 to 4 sentences each.
   top_risks: up to 5 short items, most serious first. next_steps: up to 6 short, concrete actions.
Answer with JSON only."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def facts(ev):
    """The fact sheet: totals, the databases, and the most severe issues, as plain lines."""
    t = ev["totals"]
    lines = [f"overall_result: {ev['overall']} ({plain.OVERALL_HELP[ev['overall']]})"]
    test = (ev.get("run") or {}).get("test")
    if test:
        lines.insert(0, f"run_type: {test['text']} The report already opens with this sentence: do not repeat it; "
                        "never say the whole migration is ready.")
    for part, key, what in ((1, "part1", "required tables"), (2, "part2", "mappings")):
        tt = t[key]
        n = sum(tt.values())
        lines.append(f"part{part}: {n} {what}: " + ", ".join(f"{v} {k}" for k, v in tt.items() if v))
    for d in ev["part1"].get("databases") or []:
        lines.append(f"part1 database: {d['source_db']} on {d['source_server']} copied to {d['target_db']} on "
                     f"{d['target_server']} ({d['required']} required tables)")
    tdb = next((d["name"] for d in ev["part2"].get("databases") or [] if d["role"] == "target"), "")
    lines.append(f"part2 target database: {tdb}")
    issues = [i for i in ev["issues"] if i["result"] in (plain.MUST_FIX, plain.DECIDE)][:settings.AI_MAX_ISSUES]
    lines.append(f"issues: {ev['totals']['issues'][plain.MUST_FIX]} Must fix, "
                 f"{ev['totals']['issues'][plain.DECIDE]} Needs a decision; most severe first:")
    for i in issues:
        where = (f"{i['source_db']}.{i['table']}" if i["part"] == 1
                 else f"{i['mapping']} ({i['source_db']}.{i['table']} -> {i['target_db']}.{i['target_table']})")
        lines.append(f"- part{i['part']} | {i['result']} | {i['check']} | {where} | {i['finding']}")
    nc = ev.get("not_checked") or []
    if nc:
        lines.append(f"not_checked: {len(nc)} items could not be checked in this run.")
    return "\n".join(lines)


def _bad(answer, fact_text):
    """Numbers and names in the answer that the facts do not contain."""
    text = " ".join([answer["executive_summary"], answer["part1_summary"], answer["part2_summary"],
                     *answer["top_risks"], *answer["next_steps"]])
    nums = set(NUMBER.findall(fact_text)) | {str(i) for i in range(11)}
    low = fact_text.lower()
    bad = [n for n in NUMBER.findall(text) if n not in nums and n.replace(",", "") not in nums]
    bad += [n for n in NAME.findall(text) if n.lower() not in low and n.lower() not in COMMON]
    return sorted(set(bad))


def _ask(fact_text, extra=None):
    client = ai._client()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"FACT SHEET\n{fact_text}"}]
    if extra:
        messages.append({"role": "user", "content": extra})
    resp = client.chat.completions.create(
        model=config.OPENAI_MODEL, messages=messages, max_completion_tokens=config.AI_MAX_OUTPUT_TOKENS,
        response_format={"type": "json_schema", "json_schema": {"name": "report_summary", "strict": True,
                                                                "schema": SCHEMA}})
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise ValueError("the answer was cut off (raise AI_MAX_OUTPUT_TOKENS)")
    data = json.loads(choice.message.content or "")
    answer = {k: (data.get(k) or "").strip() if isinstance(data.get(k), str) else [str(x).strip() for x in data.get(k) or []
                                                                                   if str(x).strip()]
              for k in SCHEMA["required"]}
    answer["top_risks"], answer["next_steps"] = answer["top_risks"][:5], answer["next_steps"][:6]
    usage = getattr(resp, "usage", None)
    return answer, {"input": getattr(usage, "prompt_tokens", None), "output": getattr(usage, "completion_tokens", None)}


def template(ev):
    """Plain sentences from the same facts: used when the AI is off, unavailable or unsure."""
    t, nc = ev["totals"], len(ev.get("not_checked") or [])
    p1, p2 = t["part1"], t["part2"]
    n1, n2 = sum(p1.values()), sum(p2.values())
    must = [i for i in ev["issues"] if i["result"] == plain.MUST_FIX]
    kinds = {}
    for i in must:
        kinds[i["check"]] = kinds.get(i["check"], 0) + 1
    top = sorted(kinds.items(), key=lambda kv: -kv[1])[:5]
    return {
        "executive_summary": (
            f"The migration check result is {ev['overall']}: {plain.OVERALL_HELP[ev['overall']]} "
            f"{t['issues'][plain.MUST_FIX]} problems must be fixed and {t['issues'][plain.DECIDE]} items need a decision."
            + (f" {nc} items could not be checked in this run." if nc else "")),
        "part1_summary": (f"Of the {n1} required tables copied from the ATNM server to RDS, {p1[plain.CORRECT]} are "
                          f"correct, {p1[plain.MUST_FIX]} must be fixed, {p1[plain.DECIDE]} need a decision and "
                          f"{p1[plain.NOT_CHECKED]} could not be checked."),
        "part2_summary": (f"Of the {n2} mappings into the new system, {p2[plain.CORRECT]} are correct, "
                          f"{p2[plain.MUST_FIX]} must be fixed, {p2[plain.DECIDE]} need a decision, "
                          f"{p2[plain.NOT_CHECKED]} could not be checked and {p2[plain.BY_DESIGN]} are not compared "
                          "by design."),
        "top_risks": [f"{n} × {k}: {plain.meaning(k)}" for k, n in top],
        "next_steps": [plain.action(k) for k, _ in top] + (["Check again the items that could not be checked."] if nc else []),
    }


def _cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(c):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(c, indent=1), encoding="utf-8")
    except OSError:
        pass


def write(ev, note=lambda *a: None):
    """The written parts for evidence `ev`: {source: ai|template, text..., why}. A test run's
    summary always opens with what the test covered (testmode.py)."""
    out = _write(ev, note)
    test = (ev.get("run") or {}).get("test")
    if test and not (out.get("executive_summary") or "").startswith(test["text"]):
        out = {**out, "executive_summary": f"{test['text']} {out.get('executive_summary') or ''}".strip()}
    return out


def _write(ev, note):
    fact_text = facts(ev)
    key = hashlib.sha256(fact_text.encode("utf-8")).hexdigest()
    cache = _cache()
    if key in cache:
        note("The summary of these facts was written before: reused (no AI call).")
        return cache[key]
    base = {"facts_hash": key, "written_at": _now()}
    if not settings.AI:
        return {**base, "source": "template", "why": "REPORT_AI=no", **template(ev)}
    state = ai.status()
    if not state["enabled"]:
        return {**base, "source": "template", "why": state["reason"], **template(ev)}
    try:
        note(f"Asking the AI ({config.OPENAI_MODEL}) to write the summary from {fact_text.count(chr(10)) + 1} lines of facts.")
        answer, usage = _ask(fact_text)
        bad = _bad(answer, fact_text)
        attempts = 1
        if bad:
            note(f"The answer used {len(bad)} things not in the facts ({', '.join(bad[:5])}); asking once more.", "warn")
            answer, usage2 = _ask(fact_text, "Rewrite your answer. These numbers or names are not in the fact sheet and "
                                             f"must not be used: {', '.join(bad)}. Use only the fact sheet.")
            usage = {k: (usage.get(k) or 0) + (usage2.get(k) or 0) for k in usage}
            bad = _bad(answer, fact_text)
            attempts = 2
        if bad:
            note("The AI still used things not in the facts: plain sentences are used instead.", "warn")
            return {**base, "source": "template", "why": f"AI answer not verified: {', '.join(bad[:8])}", **template(ev)}
        out = {**base, "source": "ai", "model": config.OPENAI_MODEL, "tokens": usage, "attempts": attempts, "why": None,
               **answer}
        cache[key] = out
        _save_cache(cache)
        return out
    except Exception as exc:    # the report never fails because of the AI
        note(f"The AI could not write the summary ({exc}): plain sentences are used instead.", "warn")
        return {**base, "source": "template", "why": f"AI unavailable: {exc}", **template(ev)}
