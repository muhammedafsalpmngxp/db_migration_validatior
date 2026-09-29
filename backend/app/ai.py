"""AI summary: one plain-words paragraph about a mapping, written from facts the app measured.

Flow for one mapping:
  1. `build_facts` turns what the app already knows - row counts, the saved data check,
     the column comparison, keys, and measured rename hints - into a short fact sheet in
     TOON style (field names once, then one line per item: about a third of the tokens of
     JSON). Only the interesting parts are listed in full; identical columns are counted.
  2. `summarize` sends the fixed instructions (app/ai_prompt.py) and the fact sheet to
     OpenAI, asking for a fixed JSON answer (structured output).
  3. Every number in the answer is checked against the fact sheet; an answer with a number
     that is not in the facts is sent back once to be rewritten, and flagged if it still is.
  4. The status (ok / review / problem / not_checked) is set here from the data check,
     never taken from the AI, and the result is saved until the data check changes.

What is never sent: connection details, whole tables or rows, and values of contact-type
columns (email, phone ...).
"""
import difflib
import hashlib
import json
import re
import threading
from datetime import datetime, timezone

from . import compare, config
from . import datacheck as dc
from .ai_prompt import ANSWER_SCHEMA, RETRY_NOTE, SYSTEM_PROMPT
from .values import is_sensitive

MAX_COLUMNS_IN_FULL = 12     # more columns than this: list only the ones with an issue
MAX_NAMES = 20               # longest list of column names written out
MAX_FINDINGS = 8             # per severity
MAX_RECODING_PAIRS = 8
MAX_RENAME_PAIRS = 6         # rename candidates measured per table
RENAME_MAX_ROWS = 2_000_000  # rename hints are skipped on bigger tables

STATUS_OF_VERDICT = {"identical": "ok", "review": "review", "problems": "problem"}
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class AiError(Exception):
    """The summary could not be written; the message says why in plain words."""


# ---- status -------------------------------------------------------------------------------

def status():
    if config.LLM_PROVIDER != "openai":
        return {"enabled": False, "provider": config.LLM_PROVIDER, "model": config.OPENAI_MODEL,
                "reason": f"LLM_PROVIDER={config.LLM_PROVIDER!r} is not supported; set LLM_PROVIDER=openai."}
    if not config.OPENAI_API_KEY:
        return {"enabled": False, "provider": "openai", "model": config.OPENAI_MODEL,
                "reason": "Add OPENAI_API_KEY to backend/.env to turn on AI summaries."}
    if not config.OPENAI_MODEL:
        return {"enabled": False, "provider": "openai", "model": "",
                "reason": "Add OPENAI_MODEL to backend/.env."}
    return {"enabled": True, "provider": "openai", "model": config.OPENAI_MODEL, "reason": None}


# ---- fact sheet ---------------------------------------------------------------------------

def _names(items, limit=MAX_NAMES):
    items = list(items)
    if not items:
        return "none"
    text = ", ".join(items[:limit])
    return text + (f", and {len(items) - limit} more" if len(items) > limit else "")


def _clean(text):
    """A validator sentence without tool-only advice, on one line."""
    text = text.replace(" (declare those under columns: in the mapping file)", "")
    return " ".join(text.split())


_QUOTED = re.compile(r"'[^']*'")


def _redact(text):
    """Remove quoted example values from a sentence about a contact-type column."""
    return _QUOTED.sub("'…'", text)


def _toon_rows(name, fields, rows):
    lines = [f"{name}[{len(rows)}]{{{','.join(fields)}}}:"]
    for r in rows:
        lines.append("  " + ",".join("" if r.get(f) is None else str(r.get(f)).replace(",", ";") for f in fields))
    return lines


def rename_hints(m, entry_of, comparison):
    """Measured: does a leftover old column hold the same values as a leftover new column?

    Pairs of the same kind of type (text with text, number with number ...) are ranked by
    name similarity and the best few are measured with one read-only query each: distinct
    values on each side, how many are shared, and how many also have the same row count.
    """
    if m.type not in ("one_to_one", "merge") or not comparison or not comparison.get("exists"):
        return []
    src_member = next((s for s in m.sources if s.role == "driving"), m.sources[0])
    se, te = entry_of(src_member), entry_of(m.targets[0])
    if not se or not te or se["locked"] or te["locked"]:
        return []
    if max(se["rows"], te["rows"]) > RENAME_MAX_ROWS:
        return []

    def family(t):
        b = t.split("(")[0].lower()
        if b in dc.TEXT:
            return "text"
        if b in ("int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money"):
            return "number"
        if b in dc.DATES:
            return "date"
        return b

    old = [r["source"] for r in comparison["rows"] if r["status"] == "source_only"]
    new = [r["target"] for r in comparison["rows"] if r["status"] == "target_only"]
    candidates = []
    for s in old:
        for t in new:
            if family(s["type"]) != family(t["type"]) or s["type"].split("(")[0].lower() in dc.NOCOMPARE:
                continue
            if is_sensitive(s["name"], t["name"]):
                continue
            a, b = compare.normalize(s["name"]), compare.normalize(t["name"])
            score = difflib.SequenceMatcher(None, a, b).ratio() + (0.5 if a and b and (a in b or b in a) else 0)
            candidates.append((score, s, t))
    candidates.sort(key=lambda x: -x[0])

    src_full = dc._full(src_member.ref.side, se["schema"], se["table"])
    tgt_full = dc._full(m.targets[0].ref.side, te["schema"], te["table"])
    hints, used_s, used_t = [], set(), set()
    for _score, s, t in candidates[:MAX_RENAME_PAIRS]:
        if s["name"] in used_s or t["name"] in used_t:
            continue
        sv = dc._raw_text(dc._q(s["name"]), s["type"])
        tv = dc._raw_text(dc._q(t["name"]), t["type"])
        try:
            r = dc._one(f"""
                WITH s AS (SELECT {sv} AS v, COUNT_BIG(*) AS n FROM {src_full} WHERE {dc._q(s['name'])} IS NOT NULL GROUP BY {sv}),
                     t AS (SELECT {tv} AS v, COUNT_BIG(*) AS n FROM {tgt_full} WHERE {dc._q(t['name'])} IS NOT NULL GROUP BY {tv})
                SELECT (SELECT COUNT(*) FROM s) AS sd, (SELECT COUNT(*) FROM t) AS td,
                       (SELECT COUNT(*) FROM s JOIN t ON s.v = t.v) AS shared,
                       (SELECT COUNT(*) FROM s JOIN t ON s.v = t.v AND s.n = t.n) AS same_count""")
        except dc.Skipped:
            break
        sd, td, shared, same = (dc._n(r[k]) for k in ("sd", "td", "shared", "same_count"))
        if not sd or shared < 0.8 * max(sd, td):
            continue
        if shared == sd == td and same == sd:
            note = "likely renamed (all values and row counts match)"
        elif shared == sd == td:
            note = "likely renamed (same values, some row counts differ)"
        else:
            note = "partly matching values"
        used_s.add(s["name"])
        used_t.add(t["name"])
        hints.append({"old": s["name"], "new": t["name"], "old_values": sd, "new_values": td,
                      "shared_values": shared, "same_row_counts": same, "note": note})
    return hints


def build_facts(m, members, row_check, saved, comparisons, keys_by_target, hints):
    """The fact sheet for one mapping, as TOON-style text."""
    srcs = [x for x in members if x["kind"] == "old"]
    tgts = [x for x in members if x["kind"] == "new"]
    lines = [
        f"table: {' + '.join(x['name'] for x in srcs)} -> {' + '.join(x['name'] for x in tgts) or 'nothing (not moved)'}",
        f"mapping: {m.type}",
    ]
    if m.note:
        lines.append(f"mapping_note: {_clean(m.note)}")

    # Row counts (live table metadata) and the mapping's rule.
    lines += _toon_rows("row_counts", ["role", "table", "rows"],
                        [{"role": x["kind"], "table": x["name"], "rows": x["rows"]} for x in members])
    if row_check:
        rc = row_check
        rule = {"match": "match", "mismatch": "mismatch", "info": "no count rule", "unknown": "cannot check",
                "excluded": "excluded", "locked": "table locked"}.get(rc["status"], rc["status"])
        extra = f", expected={rc['expected']}, actual={rc['actual']}, difference={rc['delta']}" \
            if rc.get("expected") is not None and rc.get("actual") is not None else ""
        lines.append(f"row_rule: {_clean(rc['rule'])}; result={rule}{extra}")

    # The data check.
    if not saved or saved.get("status") in ("skipped", "error"):
        reason = (saved or {}).get("headline") or "The value check has not been run for this table."
        lines.append(f"data_check: verdict=not_checked, reason={_clean(reason)}")
    else:
        key = saved.get("key")
        matched_on = f"{key['source']} -> {key['target']}" if key else "whole rows (no column is unique on both sides)"
        lines.append(f"data_check: verdict={saved['status']}, matched_on={matched_on}")
        r = saved.get("rows") or {}
        if saved.get("method") == "key":
            parts = [f"old={r.get('source')}", f"new={r.get('target')}", f"matched={r.get('matched')}",
                     f"missing={r.get('missing_in_target')}", f"extra={r.get('extra_in_target')}",
                     f"lost_or_changed={r.get('problem_rows')}"]
            if r.get("target_null_keys"):
                parts.append(f"extra_with_empty_key={r['target_null_keys']}")
            lines.append("rows: " + ", ".join(parts))
            for x in saved.get("missing_by_source") or []:
                lines.append(f"missing_from: {x['table']}={x['rows']}")
            if saved.get("missing_examples"):
                lines.append("missing_examples: " + _names([x["key"] for x in saved["missing_examples"]], 6))
        elif r:
            parts = [f"old={r.get('source')}", f"new={r.get('target')}", f"identical_rows={r.get('identical_rows')}",
                     f"only_in_old={r.get('only_in_source')}", f"only_in_new={r.get('only_in_target')}"]
            if r.get("identical_rows_ignoring_recoded") is not None:
                parts.append(f"identical_rows_apart_from_review_columns={r['identical_rows_ignoring_recoded']}")
            lines.append("rows: " + ", ".join(parts))

        cols = saved.get("columns") or []
        issues = [c for c in cols if c["verdict"] != "identical"]
        shown = cols if len(cols) <= MAX_COLUMNS_IN_FULL else issues
        if saved.get("method") == "key":
            fields = ["old", "new", "result", "identical", "lost", "different", "recoded", "empty_old", "empty_new"]
            rows = [{"old": c["source"], "new": c["target"], "result": c["verdict"],
                     "identical": c["buckets"]["identical"], "lost": c["buckets"]["lost"],
                     "different": c["buckets"]["different"], "recoded": c["buckets"]["recoded"],
                     "empty_old": c["nulls"]["source"] + c["nulls"]["source_blanks"], "empty_new": c["nulls"]["target"]}
                    for c in shown if c.get("buckets")]
        else:
            fields = ["old", "new", "result", "values_only_in_old", "values_only_in_new", "empty_old", "empty_new"]
            rows = [{"old": c["source"], "new": c["target"], "result": c["verdict"],
                     "values_only_in_old": (c.get("multiset") or {}).get("only_in_source", 0),
                     "values_only_in_new": (c.get("multiset") or {}).get("only_in_target", 0),
                     "empty_old": c["nulls"]["source"] + c["nulls"]["source_blanks"], "empty_new": c["nulls"]["target"]}
                    for c in shown]
        if rows:
            lines += _toon_rows("columns", fields, rows)
        if len(shown) < len(cols):
            lines.append(f"other_columns: {len(cols) - len(shown)} more compared columns, all identical")
        for c in cols:
            if c.get("lookup"):
                lk = c["lookup"]
                lines.append(f"lookup: {c['source']} -> {c['target']} was translated back through "
                             f"{lk['schema']}.{lk['table']}.{lk['column']} before comparing")

        by_sev = {"error": [], "review": [], "info": []}
        for f in saved.get("findings", []):
            text = _clean(f["text"])
            if f.get("column") and is_sensitive(f["column"]):
                text = _redact(text)
            by_sev.setdefault(f["severity"], []).append(text)
        for sev, label in (("error", "problems"), ("review", "review"), ("info", "notes")):
            items = by_sev.get(sev) or []
            if items:
                lines.append(f"{label}[{len(items[:MAX_FINDINGS])}]:")
                lines += [f"  - {t}" for t in items[:MAX_FINDINGS]]

        for c in cols:
            rec = c.get("recoding")
            if not rec or is_sensitive(c["source"], c["target"]):
                continue
            lines.append(f"recoding {c['source']} -> {c['target']}: {rec['distinct_source_values']} different old "
                         f"values; old values stored as more than one new value: {rec['inconsistent']}")
            lines += _toon_rows(f"recoding_examples[{c['source']}]", ["old_value", "new_value", "rows"],
                                [{"old_value": p["source"], "new_value": p["target"], "rows": p["rows"]}
                                 for p in rec["pairs"][:MAX_RECODING_PAIRS]])

    # Structure: the column comparison of each new table.
    for comp in comparisons:
        if not comp.get("exists") or not comp.get("summary"):
            lines.append(f"structure {comp['target']}: new table cannot be read (missing or locked)")
            continue
        s = comp["summary"]
        crow = comp["rows"]
        same = [r["target"]["name"] for r in crow if r["status"] == "match"]
        renamed = [f"{r['source']['name']} -> {r['target']['name']}"
                   + (" (guessed from naming)" if r["match"] == "inferred" else "")
                   for r in crow if r["status"] == "renamed"]
        changed = []
        for r in crow:
            if r["status"] != "changed":
                continue
            what = []
            if "type" in r["diffs"]:
                what.append(f"type {r['source']['type']} -> {r['target']['type']}")
            if "nullable" in r["diffs"]:
                what.append("may be empty" if r["target"]["nullable"] else "may no longer be empty")
            if "pk" in r["diffs"]:
                what.append("unique-key role changed")
            changed.append(f"{r['source']['name']} -> {r['target']['name']}: {'; '.join(what)}")
        only_old = [f"{r['source']['name']} ({r['source']['type']})" for r in crow if r["status"] == "source_only"]
        only_new = [f"{r['target']['name']} ({r['target']['type']})" for r in crow if r["status"] == "target_only"]
        lines.append(f"structure {comp['target'].replace('T.', '', 1)}: old_columns={s['source_columns']}, "
                     f"new_columns={s['target_columns']}, type_changes={s['type_changes']}, "
                     f"empty_allowed_changes={s['nullable_changes']}")
        lines.append(f"  same_columns: {_names(same)}")
        if renamed:
            lines.append(f"  renamed: {_names(renamed)}")
        if changed:
            lines.append(f"  changed: {_names(changed, 12)}")
        lines.append(f"  only_in_old: {_names(only_old)}")
        lines.append(f"  only_in_new: {_names(only_new)}")

    if hints:
        lines += _toon_rows("rename_hints", ["old", "new", "old_values", "new_values", "shared_values",
                                             "same_row_counts", "note"], hints)

    # Keys of each new table.
    for tname, keys in keys_by_target:
        if keys is None:
            lines.append(f"keys {tname}: cannot be read (table locked)")
            continue
        pk = next((k for k in keys["keys"] if k["kind"] == "primary"), None)
        # The same link declared more than once is shown once, with how many rules declare it.
        links = {}
        for f in keys["foreign_keys"]:
            if f["direction"] == "outgoing":
                text = (f"{', '.join(f['columns'])} -> {f['referenced']['schema']}.{f['referenced']['table']} "
                        f"({f['referenced']['rows']} rows)")
                links[text] = links.get(text, 0) + 1
        out = [t if n == 1 else f"{t} [declared by {n} identical rules]" for t, n in links.items()]
        incoming = [f"{f['parent']['schema']}.{f['parent']['table']}" for f in keys["foreign_keys"]
                    if f["direction"] == "incoming"]
        lines.append(f"keys {tname}: primary_key={', '.join(pk['columns']) if pk else 'none'}")
        lines.append(f"  links_to_other_lists: {_names(out, 10)}")
        if incoming:
            lines.append(f"  used_by_other_tables: {_names(incoming, 10)}")
    return "\n".join(lines)


# ---- number check -------------------------------------------------------------------------

def _numbers(text):
    return {n.replace(",", "") for n in NUMBER.findall(text)}


def unverified_numbers(answer, facts):
    """Numbers in the answer that do not appear in the facts (small counts 0-10 allowed)."""
    allowed = _numbers(facts) | {str(i) for i in range(11)}
    used = _numbers(" ".join([answer["verdict"], answer["summary"], *answer["worth_checking"]]))
    return sorted(n for n in used if n not in allowed and n.lstrip("0") not in allowed)


# ---- OpenAI -------------------------------------------------------------------------------

def _client():
    from openai import OpenAI  # imported here so the app starts without the package

    return OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL,
                  timeout=config.AI_TIMEOUT, max_retries=2)


def _ask(client, messages, structured=True):
    """One chat call. Returns (answer dict, usage dict)."""
    import openai

    if structured:
        fmt = {"type": "json_schema", "json_schema": {"name": "table_summary", "strict": True, "schema": ANSWER_SCHEMA}}
    else:
        fmt = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(
            model=config.OPENAI_MODEL, messages=messages, response_format=fmt,
            max_completion_tokens=config.AI_MAX_OUTPUT_TOKENS,
        )
    except openai.AuthenticationError as exc:
        raise AiError("OpenAI rejected the API key. Check OPENAI_API_KEY in backend/.env.") from exc
    except openai.PermissionDeniedError as exc:
        raise AiError(f"This API key is not allowed to use the model {config.OPENAI_MODEL!r}.") from exc
    except openai.NotFoundError as exc:
        raise AiError(f"OpenAI does not know the model {config.OPENAI_MODEL!r}. Check OPENAI_MODEL in backend/.env.") from exc
    except openai.RateLimitError as exc:
        raise AiError("OpenAI's rate limit or quota was reached. Try again in a minute, or check the account's "
                      "billing.") from exc
    except openai.APITimeoutError as exc:
        raise AiError(f"OpenAI did not answer within {config.AI_TIMEOUT} seconds. Try again.") from exc
    except openai.APIConnectionError as exc:
        raise AiError("Could not reach OpenAI. Check the internet connection or proxy.") from exc
    except openai.BadRequestError as exc:
        if structured and ("response_format" in str(exc) or "json_schema" in str(exc)):
            return _ask(client, messages, structured=False)   # model without schema support
        raise AiError(f"OpenAI refused the request: {exc}") from exc
    except openai.APIError as exc:
        raise AiError(f"OpenAI error: {exc}") from exc

    choice = resp.choices[0]
    msg = choice.message
    if getattr(msg, "refusal", None):
        raise AiError(f"The model declined to answer: {msg.refusal}")
    if choice.finish_reason == "length":
        raise AiError("The answer was cut off. Raise AI_MAX_OUTPUT_TOKENS in backend/.env and try again.")
    try:
        data = json.loads(msg.content or "")
    except ValueError as exc:
        raise AiError("The model did not return a readable answer. Try again.") from exc
    verdict, summary, checks = data.get("verdict"), data.get("summary"), data.get("worth_checking")
    if not isinstance(verdict, str) or not isinstance(summary, str) or not isinstance(checks, list):
        raise AiError("The model's answer was missing a part. Try again.")
    answer = {"verdict": verdict.strip(), "summary": summary.strip(),
              "worth_checking": [str(x).strip() for x in checks if str(x).strip()][:4]}
    usage = getattr(resp, "usage", None)
    return answer, {"input": getattr(usage, "prompt_tokens", None), "output": getattr(usage, "completion_tokens", None)}


def summarize(facts):
    """Ask the model for the summary; rewrite once if it used numbers not in the facts."""
    if not status()["enabled"]:
        raise AiError(status()["reason"])
    client = _client()
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "FACT SHEET\n" + facts}]
    answer, usage = _ask(client, messages)
    bad = unverified_numbers(answer, facts)
    attempts = 1
    if bad:
        messages += [{"role": "assistant", "content": json.dumps(answer)},
                     {"role": "user", "content": RETRY_NOTE.format(numbers=", ".join(bad))}]
        answer2, usage2 = _ask(client, messages)
        attempts = 2
        usage = {k: (usage.get(k) or 0) + (usage2.get(k) or 0) for k in ("input", "output")}
        bad2 = unverified_numbers(answer2, facts)
        if len(bad2) <= len(bad):
            answer, bad = answer2, bad2
    return answer, usage, bad, attempts


# ---- saved summaries ----------------------------------------------------------------------

class Store:
    """The last summary per mapping, in memory and in a JSON file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.items = {}

    def get(self, mapping_id):
        return self.items.get(mapping_id)

    def put(self, result):
        with self.lock:
            self.items[result["mapping"]] = result
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.items, default=str), encoding="utf-8")
            except OSError:
                pass


def make_result(mapping_id, check, facts, answer, usage, bad, attempts):
    verdict = (check or {}).get("status")
    return {
        "mapping": mapping_id,
        "status": STATUS_OF_VERDICT.get(verdict, "not_checked"),
        "verdict": answer["verdict"],
        "summary": answer["summary"],
        "worth_checking": answer["worth_checking"],
        "numbers_verified": not bad,
        "unverified_numbers": bad,
        "attempts": attempts,
        "model": config.OPENAI_MODEL,
        "tokens": usage,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_check_at": (check or {}).get("checked_at"),
        "facts": facts,
        "facts_hash": hashlib.sha256(facts.encode("utf-8")).hexdigest()[:16],
    }


def approx_tokens(text):
    """A rough token count (about 4 characters per token) for display only."""
    return max(1, round(len(text) / 4))
