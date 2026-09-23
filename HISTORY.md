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

## Switched from Postgres to Microsoft SQL Server

`utils/db.py` now talks to SQL Server over ODBC (`pyodbc`) instead of `psycopg2`:
`INFORMATION_SCHEMA` for tables/columns/primary keys, `sys.indexes` + `sys.index_columns`
for index definitions, `SELECT TOP (n)` instead of `LIMIT`, `?` parameters, and
`db.quote_ident` (bracket quoting with an identifier whitelist) in place of
`psycopg2.sql.Identifier`. `run_select` also rejects the T-SQL specific write paths
(`EXEC`, `sp_`/`xp_`, `BULK`, `OPENROWSET`, `BACKUP`, `INTO`).

`config.py` builds an ODBC connection string from `MSSQL_*` settings: SQL login or
`MSSQL_TRUSTED=yes` for Windows auth, `host,port` or `host\INSTANCE`, and an
`Encrypt` / `TrustServerCertificate` pair that is only emitted for the modern
`ODBC Driver NN for SQL Server` (the legacy driver rejects those keywords). When
`MSSQL_DRIVER` is unset the newest installed driver is picked automatically.

`setup_test_data.py` plants the same differences in T-SQL (`bit`, `decimal(10,2)` ->
`float`, one statement per `execute()`, `SINGLE_USER WITH ROLLBACK IMMEDIATE` before
`DROP DATABASE`). The Streamlit sidebar gained a driver dropdown, a Windows-auth toggle
and a trust-certificate toggle. Verified against a live SQL Server: schema, row counts,
ordered row fetch, guarded SELECT.

## Pointed at the real databases

Defaults are now `AppMasterDB_UAT` (A) and `AlTasnimBI` (B) on the RDS SQL Server named
in `.env`. Two things broke on contact with a real schema and were fixed:

- `db.quote_ident` rejected legitimate delimited names (`2026_Well_Delivery_Scope_Well_Type`,
  `Project Attribute definition`, `WorkMeasurementReport(C)`). SQL Server accepts any
  character inside `[ ]`, so the whitelist was replaced by the rule that actually holds:
  escape `]`, refuse only empty, over-long or control-character names.
- `app.py` read its sidebar defaults from the environment before `config.py` had loaded
  `.env`, so every field fell back to its built-in default and the run then connected to
  `localhost`. The app now calls `load_dotenv()` at the top, before the widgets.

Verified end to end with the mock provider: 126 tables in A, 27 in B, 5 common, structural
diff and two row level comparisons in 50s.

## Schema-aware pairing: dbo.task_daily vs well.task_daily

The two databases do not agree on where a table lives - A keeps all 126 tables in `dbo`,
B spreads 85 over `dbo`, `well`, `ref`, `dsq`, `bridge`, `core`, `project`, `wbs` and
`test` - so a single `MSSQL_SCHEMA` could not describe both sides.

`db.fetch_schema` now reads every user schema by default (`MSSQL_SCHEMA` empty; set one
name or a comma separated list to narrow) and keys each table `schema.table`, keeping its
real `schema` and `table` on the entry. `differ.pair_by_name` then re-keys both sides on
the bare table name when that name is unique within its own database, so `dbo.task_daily`
pairs with `well.task_daily`; a name that occurs in several schemas of the same database
stays qualified, because nothing can say which one was meant. Every query still runs
against the real object: `tools._locate` resolves the pair back to (schema, table) per
side, and row comparisons report `dbo.task_daily in A vs well.task_daily in B`.

Pairing is schema-agnostic in both directions: `dbo.x` with `well.x`, `ref.x` with
`core.x`, any schema with any other. When a name exists in several schemas of the *same*
database (B has `plant_description` and `well_location` twice) it is genuinely ambiguous
and stays qualified - unless the user picks one of them in the UI, which `pair_by_name`
takes as the answer (`chosen_a` / `chosen_b`) and pairs normally. The pickers therefore
send qualified names through to the run.

Verified: selecting `task_daily` compares `dbo.task_daily` (A) against `well.task_daily`
(B) - 110,360 rows each, 3 columns only in A, 4 only in B, 16 type changes, in 7.7s.

## The agent matches the tables when you do not

Leaving both pickers empty used to mean "compare everything, pair on the name" - which
found 8 pairs out of 127 tables in A and 88 in B, because the two databases renamed as
well as moved their tables. Matching is now a step of its own, `MatchTablesNode`, between
introspection and the diff:

1. `differ.match_tables` pairs deterministically - `exact` name first, then `normalized`
   (case, underscores and spaces folded away: `PlantDescription` = `plant_description`),
   schema-agnostic in both directions, each table used at most once.
2. Whatever is left over goes to the LLM, which is the part that needs judgement. It sees
   only the two leftover lists and returns proposed pairs; every proposal is checked back
   against those lists, so the model can suggest a pair but cannot invent a table, and a
   failure here downgrades to name matching instead of killing the run.
3. `differ.apply_matches` re-keys both sides so a pair shares one key. Unmatched tables
   keep their qualified name and show up as one-sided.

The agent is asked only when the user did not name the tables - an explicit pick is an
instruction, not a starting point. A new **Table matching** tab shows every pair with how
it was matched and why, plus both unmatched lists.

Verified on the live databases with nothing selected: 36 pairs - 8 exact, 24 normalized,
4 from the agent (`dbo.PlantDescription` <-> `core.plant_description`,
`dbo.ProductionIncentiveData` <-> `core.production_incentive`, `dbo.ProjectDirectors` <->
`project.project_director`, `dbo.WBS_Master_Tracker_` <-> `wbs.WBS_master`) - leaving 91
tables only in A and 52 only in B. 4 LLM calls, 22,097 tokens, 135s.

## Made a run survive a 71 million row table, and cost a third of the tokens

A full run against the real databases took 246s, burnt 3 of its 8 agent steps on
`HYT00` query timeouts, and spent 84,420 tokens (82,431 of them input). Three separate
causes, all now fixed.

**Blocking and timeouts.** Reads ran at READ COMMITTED, so a comparison queued behind
whoever was writing to UAT - `sample_rows` timed out at 30s on `TOP (10)`. Reads are now
READ UNCOMMITTED (`MSSQL_DIRTY_READS=no` to opt out): nothing here writes and every read
is rolled back. The statement timeout is its own setting (`MSSQL_QUERY_TIMEOUT`, 120s)
rather than sharing the login timeout, and a timeout now raises `db.QueryTimeout` so a
caller can retry smaller instead of failing. That same `sample_rows` now takes 0.5s.

**count(*) on everything.** Introspection counted rows table by table; one of those
tables has 71,160,133 rows. Counts now come from `sys.partitions` in one read per
database - `sys.dm_db_partition_stats` would need VIEW DATABASE PERFORMANCE STATE, which
the RDS login does not have. Introspection went 65.6s + 39.9s to 3.1s + 2.7s.

**Tables too big to diff row by row.** `compare_table_data` refuses a table over
`ROW_COMPARE_MAX_ROWS` (200k) and says what to do instead - an aggregate through
`run_sql`, or `force: true` to compare the first `limit` rows by key - which the tool
catalog now documents. It returns in 0.00s where it used to hang for minutes. When a read
does time out, one retry at 200 rows follows and the observation says plainly that the
comparison is partial.

**Tokens.** The schema summary was ~20,000 characters and was re-sent on every single
call - about 60% of a whole run's input. `summarize_schema_diff` now spends detail where
decisions are made (12 changed tables, 20 names per list, 5 type changes each) and counts
the rest: 19,958 -> 6,116 characters. Observations are clipped in the prompts as well
(1,200 chars per step for decide, 2,500 for the report) and `sample_rows` trims each row
to 400 characters, because a 44 column row dump was costing thousands of tokens on every
later call. The full text stays in the shared store for the UI and the log. Measured
effect on a decide call: 6,390 -> 2,652 input tokens.

## Tried and removed: table matching as a typed decision (TypeSafe Jev)

Matching tables is the one LLM call here whose answer is not prose - *which table in B is
this table in A, if any?* is a typed choice - so it was wired to Jev, TypeSafe's System
One model: one choice question per unmatched table, candidates plus `none` as options,
answered in a single pass with a probability per option. It worked (95 questions in 3
requests against a stubbed endpoint, options capped, weak matches rejected by a
confidence gate) and it was removed the same day.

The reason is the data, not the model. Matching sends table and column names - the shape
of the business - to a third party, and adding another vendor to that list was not worth
a cheaper call. Removed: `utils/decide.py`, the `_match_with_jev` path, the `TYPESAFE_*`
and `JEV_*` settings, and the confidence column. Matching is back to the chat model.

Worth keeping in mind: the schema summary already goes to OpenAI on every run. If the
names themselves are the concern, the answer is a local or self-hosted model through
`OPENAI_BASE_URL`, not a choice between vendors.

## Row payloads encoded TOON-style

Rows are the bulkiest thing that goes into a prompt and the most wasteful shape: a list
of dicts repeats every column name on every row. Measured on `dbo.task_daily`, 43 columns
and 10 rows: ~3,300 tokens as indented JSON, ~2,850 as `repr(dict)`. The earlier fix
capped that by cutting each row at 400 characters, which was cheap but lost data - a wide
row was truncated halfway through.

`utils/toon.py` names the fields once and writes the values as rows, in the style of TOON
(Token-Oriented Object Notation) - about 40 lines, no dependency, only the tabular part,
which is where the saving is:

    sample[10]{id,ActionOn,task_code,...}:
      1416,2024-12-03,...

Same ten rows now cost ~1,020 tokens with nothing truncated: a third of `repr(dict)`, and
the same budget the clipping used to spend on half the data. Rows whose fields differ
fall back to one `- key: value` line each, because the tabular form only pays off when
every row has the same shape. Used by `sample_rows` and `run_sql`, both of which return
uniform rows; the schema summary is left alone, being neither uniform nor repetitive.

## Stopped the agent paying for our constraints

Three failures in a row against the real databases, all of them avoidable before the
query left the process.

**A numeric aggregate over a text column.** `ActivityTaskPlan.weightage` is nvarchar(100)
in A and float in B (so are `remaining_duration`, `schedule_id`, `project_id`,
`Time_Stamp`). `SUM(weightage)` scanned for 39s and came back with "Error converting data
type nvarchar to float", which told the agent nothing. `_type_problems` now refuses it in
0.00s and names both types - the drift *is* the finding it was looking for.

**An unbounded aggregate over 71 million rows.** `GROUP BY project_id` with no WHERE
timed out at 120s. `_scan_problem` refuses an aggregate with no WHERE over a table above
`SQL_SCAN_MAX_ROWS` (2M) and names the columns that are actually indexed, because "add a
WHERE" is useless advice otherwise: a range over an unindexed column on a table that size
still scans it (verified - `WHERE created_at >= ...` timed out too). If the table has no
index at all, it says so rather than giving advice that cannot work. `force: true`
overrides.

**Our own wrapper.** `run_select` capped results with `SELECT TOP (n) * FROM (<query>) AS
_agent_sub`, and a derived table demands that every column be named and forbids ORDER BY.
So `SELECT project_id, COUNT(*) ... GROUP BY project_id` failed with "No column name was
specified for column 2" - a rule the agent had no reason to expect, and our rule, not
T-SQL's. The query now runs exactly as written and the cap is applied while reading rows
(`fetchmany`); an unaliased expression comes back as `column_2`. ORDER BY works. The tool
catalog no longer claims otherwise.

The Errors tab also stopped overstating: a tool call the agent worked around is now
listed under "recovered", separately from anything that cost the run, and the Errors
metric counts only the latter.
