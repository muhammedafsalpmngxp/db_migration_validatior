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

The selected table is in the URL (`?table=A.dbo.crews`), so a view can be shared or
bookmarked, and Back / Forward work.

### How columns are paired

In order, each round over the columns still unpaired: declared in the mapping file
(`columns:`), identical name, same name in another case, same name ignoring case /
underscores / spaces, and finally **inferred** from the target's naming convention
(`Supervisor` -> `supervisor_id`, `id` -> `company_id`, `Name` -> `emp_name`). Inferred pairs
are guesses and are labelled as such; anything the rules cannot see
(`Description` -> `crew_type_name`) belongs in the mapping's `columns:`.

### API

| Endpoint | Returns |
|---|---|
| `GET /api/health` | whether each database is reachable |
| `GET /api/scope?refresh=true` | the in-scope source tables with live stats, targets and count status |
| `GET /api/compare?table=A.dbo.Company` | mapping, live row counts and column comparison |
| `GET /api/row-count?table=T.ref.crew` | exact `COUNT_BIG(*)` of one table in the plan |

Row counts come from `sys.partitions` (instant even for the 73 million row
`ActivityTaskPlan`); the exact count is on demand because it reads the whole table.
