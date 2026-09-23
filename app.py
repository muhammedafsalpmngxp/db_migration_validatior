"""Streamlit frontend.

    streamlit run app.py
"""
import json
import os

import pandas as pd
import streamlit as st

# The sidebar reads its defaults from the environment and then writes the chosen values
# back, so .env has to be loaded here - before the widgets - or every field would fall
# back to its built-in default and silently overwrite what .env says. `override` means
# this project's .env also beats anything the machine exports: a stale OPENAI_API_KEY in
# the system environment used to win, with nothing in the UI to show which key was used.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=True)
    os.environ["DBCOMPARE_ENV_LOADED"] = "1"  # config.py must not re-apply it on reload
except ImportError:  # dotenv is optional
    pass

st.set_page_config(page_title="Database comparison agent", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; max-width: 1180px;}
      .stMarkdown code {font-size: 0.86rem;}
      /* Panels carry their own background, so they must carry their own text colour too:
         inheriting it makes them invisible under the dark theme. */
      .verdict {border-left: 3px solid #2d6a4f; padding: .55rem .9rem; background:#f2f7f4;
                color:#14231c; font-size: .95rem; margin-bottom: .4rem;
                word-break: break-word;}
      .verdict.warn {border-left-color:#a8501a; background:#fbf4ee; color:#3a2313;}
      @media (prefers-color-scheme: dark) {
        .verdict {background:#16241d; color:#dce8e2; border-left-color:#4f9a77;}
        .verdict.warn {background:#2b1d13; color:#f0dccb; border-left-color:#d1834a;}
        .runhead {background:#11161c; color:#e6eaee; border-color:#2b333c;}
      }
      .stDataFrame {font-size: .85rem;}

      /* LLM call pipeline, CI style: step list left, payload right */
      .runhead {display:flex; align-items:center; gap:.5rem; padding:.5rem .7rem;
                border:1px solid #e3e6e8; border-bottom:none; border-radius:.4rem .4rem 0 0;
                background:#fff; font-size:.95rem;}
      .runhead .tick.ok {color:#16b981; font-weight:700;}
      .runhead .dur, .panehead .dur {margin-left:auto; color:#8a9199; font-size:.8rem;
                                     font-variant-numeric:tabular-nums;}
      .panehead {display:flex; align-items:center; gap:.4rem; padding:.7rem .9rem;
                 background:#1f2a44; color:#f2f5fa; border-radius:.4rem .4rem 0 0;
                 font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.85rem;}
      .panehead.failed {background:#4a2020;}
      .panehead .sep {color:#7f8aa3;}
      .panehead .sub {color:#9fb0cc; font-weight:400;}
      /* the step buttons: flat rows, not pills */
      div[data-testid="column"]:first-child .stButton button {
        text-align:left; justify-content:flex-start; border-radius:0;
        border:1px solid #e3e6e8; border-top:none; font-size:.88rem; padding:.45rem .7rem;
        font-variant-numeric:tabular-nums;
      }
    </style>
    """,
    unsafe_allow_html=True,
)



def arrow_safe(df):
    """Stringify mixed-type object columns so Arrow serialization never fails."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = df[col].map(lambda v: "" if v is None else str(v))
    return df


PIPELINE_TITLES = {
    "decide": "Decide",
    "report": "Compose report",
    "supervise": "Supervise",
    "llm": "LLM",
}


def build_pipeline(detail, history):
    """Interleave the LLM calls with the tool calls they triggered, in execution order.

    Each `decide` call is followed by the tool the agent chose, so the pipeline reads the
    way the run actually happened: decide -> tool -> decide -> ... -> report -> supervise.
    Retries show up as consecutive `decide` steps, which is what we want to see.
    """
    steps = []
    pending = list(history)
    for call in detail:
        label = call.get("label") or "llm"
        steps.append(
            {
                "kind": "llm",
                "name": PIPELINE_TITLES.get(label, label.title()),
                "duration": call["seconds"],
                "ok": not call.get("error"),
                "subtitle": (
                    f"{call['provider']} · {call['model']} · "
                    f"{call['total_tokens']:,} tokens "
                    f"(in {call['prompt_tokens']:,} / out {call['completion_tokens']:,})"
                ),
                "panes": {
                    "Prompt": call.get("prompt", ""),
                    "Response": call.get("error") or call.get("response", ""),
                },
            }
        )
        # Every decide that succeeded picked the next tool; the final one picked `finish`,
        # by which point `pending` is empty. A failed attempt is a retry and picks nothing.
        if label == "decide" and not call.get("error") and pending:
            h = pending.pop(0)
            steps.append(
                {
                    "kind": "tool",
                    "name": h["tool"],
                    "duration": h.get("seconds", 0),
                    "ok": not str(h.get("observation", "")).startswith("TOOL ERROR"),
                    "subtitle": f"local tool call · {json.dumps(h.get('params') or {})}",
                    "panes": {
                        "Observation": h.get("observation", ""),
                        "Reasoning": h.get("thinking") or h.get("reason") or "",
                    },
                }
            )
    return steps


def _clock(seconds):
    """mm:ss the way a CI run shows a step duration, with a tenth under the minute."""
    seconds = float(seconds or 0)
    if seconds < 60:
        return f"00:{seconds:04.1f}"
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def render_pipeline(steps, key="pipe"):
    """CI-style view: the step list on the left, the selected step's payload on the right."""
    if not steps:
        st.info("No LLM calls were recorded for this run.")
        return

    state_key = f"{key}_selected"
    if st.session_state.get(state_key, 0) >= len(steps):
        st.session_state[state_key] = 0
    selected = st.session_state.get(state_key, 0)

    left, right = st.columns([1, 2], gap="medium")

    with left:
        st.markdown(
            f"<div class='runhead'><span class='tick ok'>&#10003;</span>"
            f"<b>run</b><span class='dur'>{_clock(sum(s['duration'] for s in steps))}</span></div>",
            unsafe_allow_html=True,
        )
        for i, s in enumerate(steps):
            mark = "✓" if s["ok"] else "✕"
            chosen = "▸ " if i == selected else ""
            if st.button(
                f"{mark}  {chosen}{s['name']}  ·  {_clock(s['duration'])}",
                key=f"{key}_btn_{i}",
                use_container_width=True,
                type="primary" if i == selected else "secondary",
            ):
                st.session_state[state_key] = i
                selected = i

    step = steps[selected]
    with right:
        st.markdown(
            f"<div class='panehead{'' if step['ok'] else ' failed'}'>"
            f"<b>{step['name']}</b> <span class='sep'>&mdash;</span> "
            f"<span class='sub'>{step['subtitle']}</span>"
            f"<span class='dur'>{_clock(step['duration'])}</span></div>",
            unsafe_allow_html=True,
        )
        names = [n for n, body in step["panes"].items() if body]
        if not names:
            st.caption("Nothing was captured for this step.")
            return
        pane = names[0]
        if len(names) > 1:
            pane = st.radio(
                "pane", names, horizontal=True, label_visibility="collapsed",
                key=f"{key}_pane_{selected}",
            )
        st.code(step["panes"][pane], language="text")


def show_table(df):
    """Full-width dataframe that works across Streamlit versions."""
    df = arrow_safe(df)
    try:
        st.dataframe(df, width="stretch", hide_index=True)
    except Exception:
        st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------- sidebar


def _odbc_drivers():
    """SQL Server ODBC drivers installed on this machine, best one first."""
    try:
        import pyodbc

        found = sorted((d for d in pyodbc.drivers() if "SQL Server" in d), reverse=True)
    except Exception:
        found = []
    return found or ["ODBC Driver 17 for SQL Server"]


with st.sidebar:
    st.subheader("SQL Server")
    host = st.text_input("Server", os.getenv("MSSQL_HOST", "localhost"),
                         help="host, or host\\INSTANCE for a named instance")
    port = st.text_input("Port", os.getenv("MSSQL_PORT", "1433"),
                         help="leave empty for a named instance")
    trusted = st.checkbox(
        "Windows authentication",
        value=str(os.getenv("MSSQL_TRUSTED", "no")).lower() in ("1", "true", "yes", "y"),
    )
    user = st.text_input("Login", os.getenv("MSSQL_USER", "sa"), disabled=trusted)
    password = st.text_input(
        "Password", os.getenv("MSSQL_PASSWORD", ""), type="password", disabled=trusted
    )
    drivers = _odbc_drivers()
    current_driver = os.getenv("MSSQL_DRIVER", drivers[0])
    driver = st.selectbox(
        "ODBC driver", drivers,
        index=drivers.index(current_driver) if current_driver in drivers else 0,
    )
    trust_cert = st.checkbox(
        "Trust server certificate",
        value=str(os.getenv("MSSQL_TRUST_CERT", "yes")).lower() in ("1", "true", "yes", "y"),
        help="needed for a local dev certificate, especially with ODBC Driver 18",
    )
    db_a = st.text_input("Database A (baseline)", os.getenv("DB_A_NAME", "AppMasterDB_UAT"))
    db_b = st.text_input("Database B (candidate)", os.getenv("DB_B_NAME", "AlTasnimBI"))

    st.subheader("Agent")
    providers = ["openai", "anthropic", "mock"]
    current = os.getenv("LLM_PROVIDER", "openai")
    provider = st.selectbox(
        "LLM provider", providers, index=providers.index(current) if current in providers else 0
    )
    default_model = {"openai": "gpt-4o", "anthropic": "claude-sonnet-5", "mock": "mock"}[provider]
    model = st.text_input("Model", os.getenv("LLM_MODEL", default_model))
    key_var = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider)
    api_key = ""
    if key_var:
        api_key = st.text_input(f"{key_var}", os.getenv(key_var, ""), type="password")
    max_steps = st.slider("Investigation budget (steps)", 2, 15, 8)

# Environment is read by config.py at import time, so set it before importing the flow.
os.environ.update(
    MSSQL_HOST=host, MSSQL_PORT=port, MSSQL_USER=user, MSSQL_PASSWORD=password,
    MSSQL_TRUSTED="yes" if trusted else "no", MSSQL_DRIVER=driver,
    MSSQL_TRUST_CERT="yes" if trust_cert else "no",
    DB_A_NAME=db_a, DB_B_NAME=db_b,
    LLM_PROVIDER=provider, LLM_MODEL=model, MAX_AGENT_STEPS=str(max_steps),
)
if api_key and key_var:
    os.environ[key_var] = api_key

import config  # noqa: E402
import importlib  # noqa: E402

importlib.reload(config)
import flow as flowmod  # noqa: E402
from flow import run_comparison  # noqa: E402
from utils import db as dbutil  # noqa: E402
from utils.observability import LOG_FILE, setup_logging  # noqa: E402

setup_logging()


# ---------------------------------------------------------------- header


st.title("Database comparison agent")
st.caption(
    f"Baseline **{db_a}** against candidate **{db_b}** — PocketFlow agent on Microsoft SQL Server"
)

@st.cache_data(show_spinner=False, ttl=300)
def list_tables(dsn, schema):
    """`schema.table` for every table of one database, cached (the page reruns a lot)."""
    return sorted(dbutil.fetch_schema(dsn, schema, with_row_counts=False))


def bare(qualified):
    """`well.task_daily` -> `task_daily`: the name the two databases agree on."""
    return qualified.split(".", 1)[-1]


def mirror_selection(source_key):
    """Keep both multiselects on the same tables, by name rather than by schema.

    A comparison only means something when the two sides look at the same table, but the
    two databases do not agree on where it lives - dbo.task_daily here, well.task_daily
    there - so the selection is mirrored on the bare table name and each side resolves it
    to its own schema.
    """
    st.session_state["tables_chosen"] = [bare(t) for t in st.session_state.get(source_key, [])]


c1, c2 = st.columns(2)
tables = {}
for col, side, name, dsn in (
    (c1, "a", db_a, config.dsn_a()),
    (c2, "b", db_b, config.dsn_b()),
):
    with col:
        try:
            dbutil.ping(dsn)
            names = list_tables(dsn, config.SCHEMA)
            tables[side] = names
            st.success(f"{name}: connected · {len(names)} tables")
        except Exception as exc:
            tables[side] = []
            st.error(f"{name}: {exc}")

chosen = st.session_state.setdefault("tables_chosen", [])
comparable = sorted({bare(t) for t in tables.get("a", [])} & {bare(t) for t in tables.get("b", [])})

picked = []
for col, side, name in ((c1, "a", db_a), (c2, "b", db_b)):
    with col:
        options = tables.get(side, [])
        # The widget owns its key, so the mirrored value is written before it renders.
        # A name the user picked explicitly is kept as-is; anything mirrored from the
        # other side resolves to this side's own object(s) of that name.
        key = f"tables_{side}"
        current = st.session_state.get(key, [])
        st.session_state[key] = [
            t for t in options if t in current or (bare(t) in chosen and not
                                                   any(bare(c) == bare(t) for c in current))
        ]
        st.multiselect(
            f"Tables to compare in {name}",
            options,
            key=key,
            on_change=mirror_selection,
            args=(key,),
            placeholder="All tables" if options else "not connected",
            help="Leave empty to compare every table. Picking on either side mirrors to the "
                 "other by table name, whatever schema it lives in; pick the qualified name "
                 "yourself when the same name exists in several schemas.",
        )
        picked += st.session_state[key]

if chosen:
    usable = [t for t in chosen if t in comparable]
    dropped = [t for t in chosen if t not in comparable]
    pairs = []
    for t in usable:
        qa = [q for q in st.session_state.get("tables_a", []) if bare(q) == t]
        qb = [q for q in st.session_state.get("tables_b", []) if bare(q) == t]
        if qa and qb and qa[0] != qb[0]:
            pairs.append(f"{qa[0]} ↔ {qb[0]}")
    st.caption(
        f"Comparing {len(usable)} table(s) present in both databases"
        + (f" · not in both, so ignored: {', '.join(dropped)}" if dropped else "")
        + (f" · paired across schemas: {', '.join(pairs)}" if pairs else "")
    )
else:
    st.caption(
        f"Comparing every table · {len(comparable)} names exist in both databases"
        " · a table is paired by name whatever schema it sits in"
    )

question = st.text_input(
    "What do you want to know?",
    "Compare both databases and tell me what changed in schema and data.",
)

run = st.button("Run comparison", type="primary")


# ---------------------------------------------------------------- run


if run:
    progress = st.status("Agent working…", expanded=True)
    lines = []

    def on_progress(msg):
        lines.append(msg)
        progress.write(msg)

    try:
        # Qualified names go through, so the run knows which object was meant when the
        # same table name exists in more than one schema.
        shared = run_comparison(
            question, on_progress=on_progress, tables=sorted(set(picked)) or None
        )
        progress.update(label="Comparison finished", state="complete", expanded=False)
        st.session_state["shared"] = shared
    except Exception as exc:
        progress.update(label="Run failed", state="error")
        # The partial store still carries the logs, errors and token usage of the run.
        st.session_state["shared"] = flowmod.LAST_RUN or None
        st.error(f"{type(exc).__name__}: {exc}")
        with st.expander("Traceback", expanded=False):
            st.exception(exc)


# ---------------------------------------------------------------- results


shared = st.session_state.get("shared")
if shared:
    diff = shared.get("schema_diff", {})
    data_diffs = shared.get("data_diffs", {})

    stats = shared.get("llm_stats", {}) or {}
    errors = shared.get("errors", []) or []

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Tables only in A", len(diff.get("tables_only_in_a", [])))
    m2.metric("Tables only in B", len(diff.get("tables_only_in_b", [])))
    m3.metric("Tables with schema changes", len(diff.get("tables_with_schema_changes", [])))
    m4.metric("Tables with data drift", sum(1 for d in data_diffs.values() if not d.get("identical")))

    u1, u2, u3, u4 = st.columns(4)
    u1.metric("LLM calls", stats.get("calls", 0),
              f"{stats.get('failures', 0)} failed" if stats.get("failures") else None,
              delta_color="inverse")
    u2.metric(
        "Tokens consumed",
        f"{stats.get('total_tokens', 0):,}" + ("*" if stats.get("estimated") else ""),
        f"in {stats.get('prompt_tokens', 0):,} / out {stats.get('completion_tokens', 0):,}"
        + (f" · {stats.get('cached_tokens', 0):,} cached" if stats.get("cached_tokens") else ""),
        delta_color="off",
    )
    u3.metric("Run time", f"{shared.get('seconds', 0)}s", f"{stats.get('seconds', 0)}s in LLM",
              delta_color="off")
    blocking_errors = [e for e in errors if not e.get("recovered")]
    u4.metric(
        "Errors", len(blocking_errors),
        f"+{len(errors) - len(blocking_errors)} recovered" if len(errors) > len(blocking_errors) else None,
        delta_color="off",
    )
    if stats.get("estimated"):
        st.caption("Token counts marked with an asterisk are estimated "
                   "(the provider returned no usage block).")

    tabs = st.tabs(["Report", "Table matching", "Schema differences", "Data differences",
                    "Agent steps", "Review", "LLM usage", "Logs", "Errors"])

    with tabs[0]:
        st.markdown(shared.get("report", "_no report_"))
        st.download_button(
            "Download report.md", shared.get("report", ""), file_name="comparison_report.md"
        )

    with tabs[1]:
        matches = shared.get("table_matches") or {}
        pairs = matches.get("pairs", [])
        if not pairs and not matches:
            st.info("This run predates table matching. Run the comparison again.")
        counts = matches.get("by_method", {})
        st.markdown(
            f"**{len(pairs)} table(s) matched** — "
            + (", ".join(f"{n} {m}" for m, n in sorted(counts.items())) or "none")
            + f" · {len(matches.get('unmatched_a', []))} only in {db_a}"
            + f" · {len(matches.get('unmatched_b', []))} only in {db_b}"
        )
        st.caption(
            "exact = same table name · normalized = same name ignoring case and "
            "underscores · agent = the model's judgement, checked against both table "
            "lists · given = you picked it"
        )
        if pairs:
            show_table(pd.DataFrame([
                {
                    "How": p["method"],
                    f"{db_a}": p["a"],
                    f"{db_b}": p["b"],
                    "Compared as": p["key"],
                    "Why": p.get("why", ""),
                }
                for p in sorted(pairs, key=lambda p: (p["method"], p["key"]))
            ]))

        u1, u2 = st.columns(2)
        for col, side, name in ((u1, "unmatched_a", db_a), (u2, "unmatched_b", db_b)):
            with col:
                names = matches.get(side, [])
                st.markdown(f"**Only in {name}** ({len(names)})")
                if names:
                    show_table(pd.DataFrame({"Table": names}))
                else:
                    st.success("Every table was matched.")

    with tabs[2]:
        only_a = diff.get("tables_only_in_a", [])
        only_b = diff.get("tables_only_in_b", [])
        def one_sided(names, where):
            """A handful of names reads fine inline; a real schema needs a searchable list."""
            if not names:
                return
            st.markdown(
                f"<div class='verdict warn'><b>{len(names)}</b> table(s) only in {where}"
                + (f": {', '.join(names)}" if len(names) <= 12 else "")
                + "</div>",
                unsafe_allow_html=True,
            )
            if len(names) > 12:
                with st.expander(f"List the {len(names)} tables only in {where}"):
                    show_table(pd.DataFrame({"Table": names}))

        one_sided(only_a, db_a)
        one_sided(only_b, db_b)

        rows = []
        for table, d in diff.get("table_diffs", {}).items():
            for c in d["columns_only_in_a"]:
                rows.append([table, c, "column only in A", "-", "-"])
            for c in d["columns_only_in_b"]:
                rows.append([table, c, "column only in B", "-", "-"])
            for t in d["type_changes"]:
                rows.append([table, t["column"], "type changed", t["a"], t["b"]])
            for n in d["nullability_changes"]:
                rows.append([table, n["column"], "nullability changed", n["a"], n["b"]])
            for x in d["default_changes"]:
                rows.append([table, x["column"], "default changed", x["a"], x["b"]])
            if d["primary_key_changed"]:
                rows.append([table, "-", "primary key changed",
                             str(d["primary_key_a"]), str(d["primary_key_b"])])
            for i in d["indexes_only_in_a"]:
                rows.append([table, "-", "index only in A", i, "-"])
            for i in d["indexes_only_in_b"]:
                rows.append([table, "-", "index only in B", "-", i])
        if rows:
            show_table(pd.DataFrame(rows, columns=["Table", "Column", "Difference", "A", "B"]))
        else:
            st.info("Common tables are structurally identical.")

        counts = [
            {"Table": t, f"{db_a} rows": d["row_count_a"], f"{db_b} rows": d["row_count_b"],
             "Delta": (d["row_count_b"] or 0) - (d["row_count_a"] or 0)}
            for t, d in diff.get("table_diffs", {}).items()
        ]
        if counts:
            st.markdown("**Row counts**")
            show_table(pd.DataFrame(counts))

    with tabs[3]:
        if not data_diffs:
            st.info("The agent did not run a row level comparison on any table.")
        for table, d in data_diffs.items():
            with st.expander(
                f"{table} — {d['modified_count']} modified, "
                f"{d['only_in_a_count']} only in A, {d['only_in_b_count']} only in B",
                expanded=not d.get("identical"),
            ):
                st.caption(f"Compared on primary key {d['primary_key']}")
                if d["modified"]:
                    st.markdown("**Modified rows**")
                    mrows = [
                        {**m["key"], "column": c, f"{db_a}": v["a"], f"{db_b}": v["b"]}
                        for m in d["modified"] for c, v in m["changes"].items()
                    ]
                    show_table(pd.DataFrame(mrows))
                if d["only_in_a"]:
                    st.markdown(f"**Only in {db_a}**")
                    show_table(pd.DataFrame(d["only_in_a"]))
                if d["only_in_b"]:
                    st.markdown(f"**Only in {db_b}**")
                    show_table(pd.DataFrame(d["only_in_b"]))
                if d.get("identical"):
                    st.success("Every compared row matches.")

    with tabs[4]:
        for i, h in enumerate(shared.get("history", []), 1):
            st.markdown(f"**Step {i} — `{h['tool']}`** {h['params'] or ''}")
            if h.get("thinking"):
                st.caption(h["thinking"])
            st.code(h["observation"], language="text")

    with tabs[5]:
        for r in shared.get("reviews", []):
            state = "approved" if r["approve"] else "sent back"
            st.markdown(f"<div class='verdict{'' if r['approve'] else ' warn'}'>"
                        f"Supervisor {state}: {r['feedback']}</div>", unsafe_allow_html=True)
        with st.expander("Run trace"):
            st.code("\n".join(shared.get("trace", [])), language="text")

    with tabs[6]:
        st.markdown(
            f"**{stats.get('calls', 0)} LLM calls** "
            f"({stats.get('failures', 0)} failed) · "
            f"**{stats.get('total_tokens', 0):,} tokens** "
            f"(prompt {stats.get('prompt_tokens', 0):,}, "
            f"completion {stats.get('completion_tokens', 0):,}) · "
            f"{stats.get('seconds', 0)}s spent waiting on the model"
        )
        detail = stats.get("calls_detail", [])
        st.markdown("**Pipeline** — pick a step to see the prompt, the reply or the "
                    "tool observation it produced.")
        render_pipeline(build_pipeline(detail, shared.get("history", [])))

        if detail:
            chart = pd.DataFrame(
                {
                    "Prompt": [c["prompt_tokens"] for c in detail],
                    "Completion": [c["completion_tokens"] for c in detail],
                },
                index=[f"{c['n']}. {c['label'] or 'llm'}" for c in detail],
            )
            st.markdown("**Tokens per call**")
            st.bar_chart(chart)
            st.markdown("**Seconds per call**")
            st.bar_chart(
                pd.DataFrame(
                    {"Seconds": [c["seconds"] for c in detail]},
                    index=[f"{c['n']}. {c['label'] or 'llm'}" for c in detail],
                )
            )

            st.markdown("**Per call detail**")
            show_table(pd.DataFrame(detail).rename(columns={
                "n": "#", "label": "Step", "provider": "Provider", "model": "Model",
                "prompt_tokens": "Prompt", "cached_tokens": "Cached",
                "completion_tokens": "Completion",
                "total_tokens": "Total", "seconds": "Seconds", "estimated": "Estimated",
                "error": "Error",
            }))

    with tabs[7]:
        logs = shared.get("logs", []) or []
        levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        chosen = st.multiselect("Level", levels, default=["INFO", "WARNING", "ERROR", "CRITICAL"])
        needle = st.text_input("Filter", "", placeholder="substring match on the message")
        visible = [
            r for r in logs
            if r["level"] in chosen and (not needle or needle.lower() in r["message"].lower())
        ]
        st.caption(f"{len(visible)} of {len(logs)} records · full log file: {LOG_FILE}")
        st.code("\n".join(r["formatted"] for r in visible) or "no log records", language="text")
        st.download_button(
            "Download run.log",
            "\n".join(r["formatted"] for r in logs),
            file_name="dbcompare_run.log",
        )

    with tabs[8]:
        if not errors:
            st.success("No exceptions were raised during this run.")

        # A tool call the agent worked around is not the same as a broken run, and
        # showing them identically makes a healthy run look alarming.
        blocking = [e for e in errors if not e.get("recovered")]
        recovered = [e for e in errors if e.get("recovered")]

        def show_errors(items):
            for e in items:
                st.markdown(
                    f"<div class='verdict warn'><b>{e['where']}</b> — "
                    f"{e['type']}: {e['message']}</div>",
                    unsafe_allow_html=True,
                )
                if e.get("traceback"):
                    with st.expander("Traceback"):
                        st.code(e["traceback"], language="text")

        if blocking:
            st.markdown(f"**{len(blocking)} error(s) that cost the run**")
            show_errors(blocking)
        if recovered:
            st.markdown(f"**{len(recovered)} tool call(s) the agent recovered from**")
            st.caption(
                "These came back to the agent as observations - it tried something else. "
                "Worth reading when a run feels slow, not a failure in themselves."
            )
            show_errors(recovered)
        failed_calls = [c for c in stats.get("calls_detail", []) if c.get("error")]
        if failed_calls:
            st.markdown("**Failed LLM calls**")
            show_table(pd.DataFrame(failed_calls)[["n", "label", "model", "seconds", "error"]])
