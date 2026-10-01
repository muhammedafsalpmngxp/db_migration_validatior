# DB Migration Validator

Validates the migration of tables and data from two source databases into one target
database on Microsoft SQL Server.

| Side | Role | Database |
|---|---|---|
| A | source | `AppMasterDB_UAT` |
| B | source | `AppMasterEngDB_Local` |
| T | target | `AlTasnimBI` |

## Two sections

The header switches between the two stages of the migration:

- **ATNM** (`/atnm`) - the client's ATNM server (over the VPN) against its copy on RDS:
  `AppMasterDB` → `AppMasterDB_UAT` and `AppMasterEngDB` → `AppMasterEngDB_Local`. Every
  table of each database is listed from the server (nothing is configured per table) and
  checked for: the table exists on RDS, the same columns with the same types, the same
  row count and the same values. The values are compared without moving data between the
  servers: each server fingerprints its own rows (SHA-256 over every value), and only
  where fingerprints differ are keys and hashes read to find the rows missing, extra or
  changed. **Required tables** (the default view, and **Check required tables**) are the
  tables the migration uses: the source tables of `mappings/migration_plan.yaml` (62 in
  `AppMasterDB`, 5 in `AppMasterEngDB`), so the list follows the plan; **All tables** /
  **Check all tables** covers every table of both databases. A run survives a VPN drop
  (tries again, then pauses and goes on by itself when ATNM answers), never replaces a
  measured result with a failed attempt, and can be **resumed** after a restart or a stop.
  A result expires when the table's row count changes after its check; tables whose counts
  keep moving are marked **Live**. For live tables a cutoff can be set in
  `mappings/atnm.yaml`. For long runs start the backend without `--reload`: a reload stops
  the run (it can then be resumed). Code in `backend/app/ATNM/` and `frontend/components/ATNM/`; settings
  `ATNM_*` in `backend/.env` (see `.env.example`); results in
  `backend/.cache/atnm_checks.json`; **Download CSV** exports the list.
- **RDS** (`/`) - the RDS sources against the target `AlTasnimBI`, described below.

## Layout

```
backend/                      FastAPI + pyodbc, read-only against SQL Server
  app/config.py               settings from backend/.env
  app/db.py                   table lists, row counts, column definitions (catalog views only)
  app/mapping.py              loads and validates the migration plan
  app/compare.py              column alignment and row count rules
  app/datacheck.py            value by value comparison (the Data check)
  app/values.py               the values panel behind a column
  app/keymap.py               key mapping check: does each foreign key point at the right row?
  app/ai.py                   AI summary: fact sheet, OpenAI call, number check
  app/ai_prompt.py            the AI's instructions and answer shape
  app/main.py                 HTTP API
  mappings/migration_plan.yaml  the migration scope and which source becomes which target
frontend/                     Next.js 16 + Tailwind; proxies /api/* to the backend
```

## Run

Needs a Python 3.12+ environment (conda), Node 20+ and the Microsoft ODBC Driver 17 or 18
for SQL Server.

```bash
conda activate <your-env>
cd backend
pip install -r requirements.txt
copy .env.example .env        # then fill in the server and login
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev                   # http://localhost:3000
```

The frontend forwards `/api/*` to `http://127.0.0.1:8000`; set `BACKEND_URL` to point it
elsewhere.

## What the UI shows

Only the 67 source tables in the migration scope (62 in `AppMasterDB_UAT`, 5 in
`AppMasterEngDB_Local`), in plan order. The mapping file says only what maps to what;
**every number is read live from SQL Server** - existence, row counts, columns, types,
size, creation date. **Refresh** reads them again.

- **Sidebar** - every in-scope source table with a status dot (counts match / differ / no
  rule / excluded), its target, and its row count. Search by source or target name, filter
  by database or to *Needs attention*. Tables of one multi-source mapping (the WMR
  transform, the crew merge, the well priority union) are grouped together.
- **Overview** (no table selected) - totals, and one row per mapping: source rows, target
  rows, difference and status. Click a row to open it.
- **Table view** (click a table) -
  - the mapping: type, every source and target with live rows / columns / size; click
    another source of the same mapping to compare that one instead
  - row counts and the mapping's rule (target = source, = sum of sources, = driving
    table); **Count exactly** runs `COUNT_BIG(*)` on each table and checks the rule again
  - the column comparison per target: each source column paired with its target column,
    type, nullability and key differences marked; filter by status or column name
  - the keys of each target: primary key, every foreign key with the table it references
    and that table's row count, and the tables that reference it; **Check references**
    counts how many rows use each key, how many referenced rows are used, and orphans

The selected table is in the URL (`?table=A.dbo.crews`), so a view can be shared or
bookmarked, and Back / Forward work.

### Data check: are the values the same?

Row counts can match while the data does not (`task_daily`: 110,939 = 110,939, yet 419
crew types and every `time_stamp` were lost). **Run data check** on a table page, or **Run
all data checks** on the overview, compares every value inside SQL Server - read-only,
cross-database, nothing pulled into Python but counts and a few examples.

1. **Rows are matched on a key** - the target's primary key paired with a source column,
   or another id column - used only when it is unique on both sides and its values really
   overlap (a renumbered id is rejected). With no usable key, rows are compared as
   whole-row fingerprints (SHA-256 multiset, so duplicates count too).
2. **Every paired column** is compared after converting the source value to the target
   type. With a key, each matched row falls into exactly one bucket per column:
   identical, case only, added, blank → NULL, **lost** (source value, target NULL),
   recoded (source does not convert, target has a value) or **different**.
3. **Recoded foreign keys are translated back**: the referenced table's column that best
   matches the source text is found (`crew_type_id` → `ref.crew_type.crew_type_code`) and the
   comparison repeated on the translated value. Recoding without a foreign key is checked
   for consistency (`'Active'` → 1, `'Inactive'` → 0) and shown for review.
4. **NULL and blank counts** of every column on both sides.

Verdicts: **Values identical**, **Needs review** (values match, but a recoding rule, case
change or value generated on load needs a person to confirm it) and **Data problems**
(rows missing or extra, values lost or changed). Every finding is a sentence with the
counts and examples. Results are kept in `backend/.cache/data_checks.json`. Mappings
above `DATA_CHECK_MAX_ROWS` (5M rows), transforms and excluded tables are not checked.

**See the values**: click any column name in the Data check table. A side panel reads the
real values live from both databases:

- **Side by side** (tables matched on a key): each source row next to its own target row,
  with the same verdict per cell (same, lost, different, recoded, missing, extra). Filter
  to only the differences, search a key or value, page through every row, 50 at a time.
- **Value counts** (every table): each distinct value with how many rows hold it in the
  source and in the target - the view for tables whose rows cannot be paired.
- **Download CSV** of the current view (up to 100,000 rows).

Columns that look like contact details (email, phone, GSM, fax) are hidden until **Show
values** is pressed. The app has no login, so anyone who can open it can press it.

### Mapping check: does every link point at the right row?

A new table often stores a number where the old one stored text: `New_Crew_code`
`'YLM-0401'` became `crew_type_id` 395, row 395 of `ref.crew_type`. **Check mapping** in the
Keys card (also part of **Run all data checks**) tests every single-column foreign key of
the target table:

1. **The source column it was made from** - the column the pairing gave it, or else the
   source text column whose values are found in the referenced table (`Unit` → `uom_id`:
   16 of 16 values are in `ref.uom.uom_code`, marked *found by its values*).
2. **The list column that holds the source value** - the one holding most of the source's
   values (`crew_type_code`, `uom_code`), or the key itself when a code was copied as it
   is. A number copied from an old list that was migrated too is compared through a label
   both lists keep: old `EquipmentType` 396 `MDG01` must be new `equipment_type` 396 `MDG01`.
3. **Row by row**, the target id is turned back into that value and compared with the
   source value of the same row. Rows are paired on the data check's key; a table without
   one is paired on its other columns that hold the same values on both sides (a pair is
   made only when those values are unique on both sides), and rows that still cannot be
   paired are compared by value counts. Every paired row lands in one bucket: **correct**,
   written differently (`'No'` → `no`: the list keeps one spelling), both empty, **not
   filled** (the value is in the list, the id is empty), **not in the list**, **wrong id**,
   id without a source value, id not in the list.
4. **Copy column**: when the target also keeps the value itself (`new_crew_code` next to
   `crew_type_id`), that the two agree on every target row.

Each foreign key row then shows its result, with counts that open the rows behind them
(source value, target id, the value that id points at). Results are kept in
`backend/.cache/key_mapping_checks.json`; the AI summary uses them too.

### AI summary: the result in plain words

The **AI summary** card at the top of each table page writes one short paragraph, plus up
to 4 things worth checking, from the facts this app measured. The AI only explains; it
decides nothing:

1. `app/ai.py` builds a **fact sheet** from the saved data check, row counts, column
   comparison and keys - TOON style (field names once, then one line per item), with only
   the columns that have an issue in full ("39 more compared columns, all identical").
   It also measures **rename hints**: a leftover old column and a leftover new column with
   the same values and row counts (`Type` -> `project_type`: 4 of 4).
2. The fact sheet and the fixed instructions in `app/ai_prompt.py` go to OpenAI
   (`OPENAI_MODEL`), which must answer in a fixed JSON shape: verdict, summary,
   worth_checking.
3. Every number in the answer is checked against the fact sheet. An answer with a number
   that is not there is sent back once to be rewritten, and flagged on the card if it still
   is. The status badge comes from the data check and the mapping check (the worse of the
   two), never from the AI.
4. The summary is saved in `backend/.cache/ai_summaries.json` and marked out of date when
   the data check or the mapping check is run again.

Never sent: connection details, whole tables or rows, and values of email / phone columns.
**See what is sent to the AI** on the card shows the exact text, and works before a key
is set. Settings in `backend/.env`: `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL`
(optional `OPENAI_BASE_URL`, `AI_TIMEOUT`, `AI_MAX_OUTPUT_TOKENS`).

### Renamed columns, checked on the data

Names alone cannot tell `start_date`, `target_start` and `actual_start` apart, so a column
left without a partner by the naming rules is matched by its data (**Find renamed columns by
data** in the Column comparison card, `app/renames.py`). Rows are paired on a key proven
unique on both sides, or on the columns that hold the same values on both sides; then every
leftover source column is compared with every leftover target column that can hold the same
kind of value, on **all** paired rows. A rename is **verified** only when 100% of the paired
rows are identical (the data check's own rule), the column holds real values, at least half
of the smaller table pairs up, and no other column matches as well. Rows on one side only
(different row counts) do not count against a rename; they are reported beside it. Anything
less - 99.99%, two equal matches, an empty or constant column - is listed for a person, with
the differing rows and a ready `columns:` line for the mapping file. Pairs the name rules
made are measured too, and shown when the data does not support them. **Re-run all data checks**
runs it too, for each mapping right after its data check (and on that check's key), so the
column comparison already shows the verified renames; mappings over `RENAME_AUTO_MAX_ROWS`
(default: the data check's own limit) are left for the button. Results are kept in
`backend/.cache/rename_checks.json`; the data check, key mapping check and AI summary keep
their own pairing.

### How columns are paired

In order, each round over the columns still unpaired: declared in the mapping file
(`columns:`), identical name, same name in another case, same name ignoring case /
underscores / spaces, and finally **inferred** from the target's naming convention
(`Supervisor` -> `supervisor_id`, `id` -> `company_id`, `Name` -> `emp_name`). A source `id`
only ever becomes the target's own key, never another table's (`id` -> `pk_id`, not
`crew_id`, on `bridge.crew_employee`). Inferred pairs
are guesses and are labelled as such; anything the rules cannot see
(`Description` -> `crew_type_name`) belongs in the mapping's `columns:`.

### API

| Endpoint | Returns |
|---|---|
| `GET /api/health` | whether each database is reachable |
| `GET /api/scope?refresh=true` | the in-scope source tables with live stats, targets and count status |
| `GET /api/compare?table=A.dbo.Company` | mapping, live row counts and column comparison |
| `GET /api/row-count?table=T.ref.crew` | exact `COUNT_BIG(*)` of one table in the plan |
| `GET /api/keys?table=T.dbo.activity_codes_norms` | primary/unique keys, foreign keys it holds and ones pointing at it, with live row counts of the linked tables |
| `GET /api/fk-check?table=...&fk=<name>` | exact counts for one foreign key: rows set, NULLs, distinct values used, orphans |
| `GET /api/data-check?mapping=crew&refresh=true` | run the data check of one mapping (without `refresh`: the saved result, or a first run) |
| `GET /api/data-check/saved?mapping=crew` | the saved result only, never runs |
| `GET /api/data-checks` | every mapping's last verdict, and the background run's progress |
| `POST /api/data-checks/run` | check every mapping in the background |
| `GET /api/data-check/values?mapping=crew&column=Code&view=rows` | the real values of one column: `view=rows` (side by side) or `counts`; `filter`, `q`, `page`, `size`, `reveal`, `format=csv` |
| `GET /api/key-mapping?mapping=activity_codes_norms&refresh=true` | run the mapping check: for every foreign key, the source column it was made from and whether each id points at the right row |
| `GET /api/key-mapping/saved?mapping=activity_codes_norms` | the saved mapping check only, never runs |
| `GET /api/key-mapping/rows?mapping=...&column=crew_type_id&filter=problems` | the rows behind one link (`filter`: `problems`, `all`, or a bucket such as `wrong`, `not_filled`); `page`, `size`, `reveal` |
| `GET /api/ai/status` | whether AI summaries are set up (key and model) |
| `GET /api/ai/facts?mapping=crew` | the exact fact sheet that is (or would be) sent to the AI; calls no AI |
| `GET /api/ai/summary/saved?mapping=crew` | the saved summary, or null; calls no AI |
| `POST /api/ai/summary` `{"mapping": "crew", "refresh": false}` | write the summary (runs the data check first if there is none) |

Row counts come from `sys.partitions` (instant even for the 73 million row
`ActivityTaskPlan`); the exact count is on demand because it reads the whole table.
A session with an open transaction that writes to a table (a load) keeps that table's
`sys.partitions` rows locked, though its rows stay readable. Such a table is then counted
directly, read uncommitted, and shown as **loading**: the count includes rows the load has
not committed yet, and its size is unknown until the load finishes
(`MSSQL_BUSY_COUNT_TIMEOUT` / `MSSQL_BUSY_COUNT_BUDGET`). Only a table whose schema is
held (truncate, index rebuild, alter) - which even an uncommitted read cannot pass - is
shown as **locked**.
