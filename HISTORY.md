# Build history

A record of how this project was put together, what was verified at each step, and the
decisions worth remembering. Newest stage last. The same sequence is in `git log`.

---

## Stage 1 — Confirm the PocketFlow API before writing against it

Installed `pocketflow` and read its source rather than working from memory. The whole
framework is one file: `BaseNode` with `prep/exec/post`, `Node` adding `max_retries`,
`wait` and `exec_fallback`, `BatchNode` running `exec` per item, `Flow` orchestrating via
the action string returned by `post`, and `>>` / `- "action" >>` for wiring.

Consequences for this project:

- `post` returns the action name, so the agent loop is expressed by returning `"tool"` or
  `"report"` from `DecideActionNode.post` — no branching logic in the flow itself.
- `Flow` warns "Flow ends: action not found" when a node with successors returns an
  unmapped action, hence the explicit `DoneNode` on the supervisor's `approve` branch.
- Retries live in `Node`, so LLM nodes get `max_retries=3` and a fallback rather than
  bespoke try/except.

## Stage 2 — Decide what the LLM is allowed to do

The core decision of the project: **the model never produces a fact.**

`utils/differ.py` computes whether two things actually differ, in plain Python. The LLM
picks which comparison to run next and writes the prose. A comparison tool that
hallucinates a row count is worse than no tool, and this split makes that impossible by
construction. It also makes most of the project testable without an API key.

## Stage 3 — Database layer (`utils/db.py`)

- `fetch_schema` reads `information_schema.columns`, `table_constraints` +
  `key_column_usage` for primary keys, and `pg_indexes`, then renders a normalised type
  string (`character varying(120)`, `numeric(10,2)`) so two databases are comparable.
- Every value goes through `jsonable()` — `Decimal` → `float`, dates → ISO strings — because
  the shared store is dumped to JSON and rendered in Streamlit.
- Read connections are opened with `set_session(readonly=True)` and a 30s
  `statement_timeout`; table and column names go through `psycopg2.sql.Identifier`.
- `run_select` is the agent-facing SQL door: single statement only, must start with
  `SELECT`/`WITH`, write and DDL keywords rejected, wrapped in an outer `LIMIT`.

## Stage 4 — Diff logic (`utils/differ.py`)

`diff_schemas` returns tables only on one side, and per common table: columns only on one
side, type changes, nullability changes, default changes, primary key changes, index
changes, row counts. `diff_rows` indexes both sides by primary key and reports rows missing
on either side plus per-column value changes.

Two details that came out of running it:

- `numeric(10,2)` vs `double precision` means `Decimal("199.99")` vs `199.99`. Value
  comparison normalises numbers and compares with a tolerance, otherwise every row in a
  retyped column looks modified.
- Keys sorted as strings put `order_id 10` before `7`. Added a type-aware sort key so the
  report reads in natural order.

## Stage 5 — Agent toolbox (`tools.py`)

Six tools: `compare_schema`, `compare_row_counts`, `compare_table_data`, `sample_rows`,
`run_sql`, `finish`. Each returns `(summary_text, raw_result)` — the short summary goes back
into the agent's context, the raw structure stays in the shared store for the UI. Keeping
those separate is what stops the context window filling up with row dumps after three steps.

## Stage 6 — Nodes and flow (`nodes.py`, `flow.py`)

```
FetchSchemas (BatchNode) -> SchemaDiff -> Decide --tool--> ExecuteTool --+
                                           ^                             |
                                           +-----------------------------+
                                           |
                                           +--report--> ComposeReport -> Supervisor --approve--> Done
                                           ^                                  |
                                           +---------------retry--------------+
```

- `FetchSchemas` is a `BatchNode` so each database is an independent unit of work.
- `DecideActionNode` replies in fenced YAML with `thinking`, `tool`, `reason`, `params`;
  an unknown tool name raises, which triggers PocketFlow's retry; the fallback returns
  `finish` so a malformed reply degrades into "write the report" instead of a crash.
- `ExecuteToolNode.exec_fallback` turns a failed tool call into an observation
  (`TOOL ERROR: ...`), so the agent can try another approach — tested explicitly.
- `SupervisorNode` checks the draft against the evidence and can send the agent back for
  more; `MAX_SUPERVISOR_RETRIES` stops it looping forever.

## Stage 7 — Sample data (`setup_test_data.py`)

Two databases, `shop_prod` and `shop_stage`, with one difference of every kind the tool
claims to detect: table only on one side, column added and dropped, type widened, numeric
type changed, NOT NULL relaxed, index added, rows added, rows removed, values changed.

Deliberate: `orders` has 12 rows on both sides but two modified rows, so a tool that only
compares counts fails the test. `order_items` has an identical schema and drifting data, so
the schema path and the data path are exercised independently.

## Stage 8 — Offline mock provider

`LLM_PROVIDER=mock` in `utils/call_llm.py` returns scripted YAML keyed off markers in the
prompts (`## TOOL CATALOG`, `## WRITE THE FINAL REPORT`, `## REVIEW TASK`, and the step
count). The entire flow — agent loop, report, supervisor approval — runs with no API key
and no cost, which is what makes the end-to-end test possible in CI.

## Stage 9 — Frontend (`app.py`) and CLI (`main.py`)

Streamlit: connection fields, provider/model/key, investigation budget; live progress
through an `on_progress` callback written into `st.status` as the flow runs; results in five
tabs — Report, Schema differences, Data differences, Agent steps (with each decision's
`thinking`), Review.

Environment variables are written from the sidebar *before* `config` is imported and
reloaded, so changing databases in the UI actually changes what the flow connects to.

## Stage 10 — Tests (`test_project.py`)

Pure diff tests always run; database tests skip themselves when Postgres is unreachable.
Coverage: row diff, schema diff, live introspection asserting each planted difference,
row-level drift, SQL guard rails, a full flow run with the mock provider, and bad-tool-call
recovery.

## Verified against a live Postgres 16

Sample databases created, agent loop completed, Streamlit rendered headlessly via
`streamlit.testing.v1.AppTest` (no exceptions, 7 dataframes, 4 metrics), full suite green.
Real output from the run:

```
- customers: columns only in A: is_active; columns only in B: loyalty_tier;
             country varchar(2) -> varchar(3); email varchar(120) -> varchar(255)
- orders: amount numeric(10,2) -> double precision; status NOT NULL -> NULL;
          indexes only in B: idx_orders_status
Row comparison of orders on primary key ['order_id']:
  rows: 12 in A, 12 in B, 9 identical
  changed {'order_id': 7}: amount: 199.99 -> 189.99
  changed {'order_id': 10}: status: shipped -> delivered
  missing in B: {'order_id': 12}   missing in A: {'order_id': 13}
```

---

## Stage 11 — Repackaged as the uv project `DBCompare`, OpenAI first

- `uv init --app`, dependencies moved from `requirements.txt` into `pyproject.toml`;
  `anthropic` became an optional extra and `pytest` a dev dependency group, so the default
  `uv sync` installs only what an OpenAI user needs. Lockfile `uv.lock` committed.
- Defaults flipped to OpenAI: `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-4o`, `.env.example`
  rewritten around `OPENAI_API_KEY`, Streamlit sidebar defaulting to OpenAI and labelling
  the key field with the variable it sets.
- `_openai()` hardened: a clear error when the key is missing instead of a `KeyError`,
  optional `OPENAI_BASE_URL` for Azure/gateway/local endpoints, and an automatic retry with
  `max_completion_tokens` for reasoning models that reject `max_tokens`.
- Added `check_setup.py`: one preflight command that reports both databases, their tables,
  and a real one-line model call — so a bad key or an unavailable model shows up in two
  seconds rather than mid-run.
- Added three offline tests that stub the OpenAI SDK: the request carries the right model
  and prompt, the `max_completion_tokens` fallback fires, and a missing key raises a useful
  message. No tokens spent.

Result: 12 tests passing under `uv run pytest`, CLI and Streamlit verified inside the uv
environment.

---

## Known limits

- `compare_table_data` pulls up to `ROW_COMPARE_LIMIT` rows per side into memory. Past a few
  hundred thousand rows, switch to block checksums (`md5(t::text)` over primary key ranges)
  and fetch only the blocks that disagree.
- Tables without a primary key cannot be compared row by row; the tool says so instead of
  guessing at a key.
- Foreign keys, check constraints, sequences, triggers, views and permissions are not yet
  read. `fetch_schema` is the single place to extend.
- Only the `public` schema is read by default (`PG_SCHEMA`), one schema per run.
