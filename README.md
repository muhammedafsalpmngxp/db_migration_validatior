# DB Migration Validator

Validates the migration of tables and data from two source databases into one target
database on Microsoft SQL Server.

| Side | Role | Database |
|---|---|---|
| A | source | `AppMasterDB_UAT` |
| B | source | `AppMasterEngDB_Local` |
| T | target | `AlTasnimBI` |

## Layout

```
backend/                      FastAPI + pyodbc, read-only against SQL Server
  app/config.py               settings from backend/.env
  app/db.py                   table lists, row counts, column definitions (catalog views only)
  app/mapping.py              loads and validates the migration plan
  app/compare.py              column alignment and row count rules
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

Row counts come from `sys.partitions` (instant even for the 73 million row
`ActivityTaskPlan`); the exact count is on demand because it reads the whole table.
