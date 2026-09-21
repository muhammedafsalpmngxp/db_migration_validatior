"""Streamlit frontend.

    streamlit run app.py
"""
import os

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Database comparison agent", layout="wide")

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; max-width: 1180px;}
      .stMarkdown code {font-size: 0.86rem;}
      .verdict {border-left: 3px solid #2d6a4f; padding: .55rem .9rem; background:#f2f7f4;
                font-size: .95rem; margin-bottom: .4rem;}
      .verdict.warn {border-left-color:#a8501a; background:#fbf4ee;}
      .stDataFrame {font-size: .85rem;}
    </style>
    """,
    unsafe_allow_html=True,
)



def show_table(df):
    """Full-width dataframe that works across Streamlit versions."""
    try:
        st.dataframe(df, width="stretch", hide_index=True)
    except Exception:
        st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------- sidebar


with st.sidebar:
    st.subheader("Connection")
    host = st.text_input("Host", os.getenv("PG_HOST", "localhost"))
    port = st.text_input("Port", os.getenv("PG_PORT", "5432"))
    user = st.text_input("User", os.getenv("PG_USER", "postgres"))
    password = st.text_input("Password", os.getenv("PG_PASSWORD", "postgres"), type="password")
    db_a = st.text_input("Database A (baseline)", os.getenv("DB_A_NAME", "shop_prod"))
    db_b = st.text_input("Database B (candidate)", os.getenv("DB_B_NAME", "shop_stage"))

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
    PG_HOST=host, PG_PORT=port, PG_USER=user, PG_PASSWORD=password,
    DB_A_NAME=db_a, DB_B_NAME=db_b,
    LLM_PROVIDER=provider, LLM_MODEL=model, MAX_AGENT_STEPS=str(max_steps),
)
if api_key and key_var:
    os.environ[key_var] = api_key

import config  # noqa: E402
import importlib  # noqa: E402

importlib.reload(config)
from flow import run_comparison  # noqa: E402
from utils import db as dbutil  # noqa: E402


# ---------------------------------------------------------------- header


st.title("Database comparison agent")
st.caption(
    f"Baseline **{db_a}** against candidate **{db_b}** — PocketFlow agent on Postgres"
)

c1, c2 = st.columns(2)
for col, name, dsn in ((c1, db_a, config.dsn_a()), (c2, db_b, config.dsn_b())):
    with col:
        try:
            dbutil.ping(dsn)
            col.success(f"{name}: connected")
        except Exception as exc:
            col.error(f"{name}: {exc}")

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
        shared = run_comparison(question, on_progress=on_progress)
        progress.update(label="Comparison finished", state="complete", expanded=False)
        st.session_state["shared"] = shared
    except Exception as exc:
        progress.update(label="Run failed", state="error")
        st.exception(exc)


# ---------------------------------------------------------------- results


shared = st.session_state.get("shared")
if shared:
    diff = shared.get("schema_diff", {})
    data_diffs = shared.get("data_diffs", {})

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Tables only in A", len(diff.get("tables_only_in_a", [])))
    m2.metric("Tables only in B", len(diff.get("tables_only_in_b", [])))
    m3.metric("Tables with schema changes", len(diff.get("tables_with_schema_changes", [])))
    m4.metric("Tables with data drift", sum(1 for d in data_diffs.values() if not d.get("identical")))

    tabs = st.tabs(["Report", "Schema differences", "Data differences", "Agent steps", "Review"])

    with tabs[0]:
        st.markdown(shared.get("report", "_no report_"))
        st.download_button(
            "Download report.md", shared.get("report", ""), file_name="comparison_report.md"
        )

    with tabs[1]:
        only_a = diff.get("tables_only_in_a", [])
        only_b = diff.get("tables_only_in_b", [])
        if only_a:
            st.markdown(f"<div class='verdict warn'>Only in {db_a}: {', '.join(only_a)}</div>",
                        unsafe_allow_html=True)
        if only_b:
            st.markdown(f"<div class='verdict warn'>Only in {db_b}: {', '.join(only_b)}</div>",
                        unsafe_allow_html=True)

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

    with tabs[2]:
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

    with tabs[3]:
        for i, h in enumerate(shared.get("history", []), 1):
            st.markdown(f"**Step {i} — `{h['tool']}`** {h['params'] or ''}")
            if h.get("thinking"):
                st.caption(h["thinking"])
            st.code(h["observation"], language="text")

    with tabs[4]:
        for r in shared.get("reviews", []):
            state = "approved" if r["approve"] else "sent back"
            st.markdown(f"<div class='verdict{'' if r['approve'] else ' warn'}'>"
                        f"Supervisor {state}: {r['feedback']}</div>", unsafe_allow_html=True)
        with st.expander("Run trace"):
            st.code("\n".join(shared.get("trace", [])), language="text")
