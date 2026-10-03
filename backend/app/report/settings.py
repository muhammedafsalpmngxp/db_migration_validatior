"""Report settings, from backend/.env (every one optional)."""
from pathlib import Path

from .. import config

_env = config._env

# Where each report run keeps its evidence and its Word and Excel files, and how many runs are kept.
REPORT_DIR = Path(_env("REPORT_DIR", config.BACKEND_DIR / ".cache" / "reports"))
KEEP = max(1, int(_env("REPORT_KEEP", "20")))
# The run in progress, so it can be resumed after a restart.
RUN_FILE = Path(_env("REPORT_RUN_FILE", config.BACKEND_DIR / ".cache" / "report_run.json"))
# The AI writes the summaries (facts only: counts, names, statuses, differences - never row
# values). no = plain sentences built from the same facts.
AI = config._flag("REPORT_AI", True)
# Most issues described to the AI (the most severe first); every issue is in the files anyway.
AI_MAX_ISSUES = int(_env("REPORT_AI_MAX_ISSUES", "60"))
# Text on the cover.
TITLE = _env("REPORT_TITLE", "Database Migration Check")
CLIENT = _env("REPORT_CLIENT", "")
PREPARED_BY = _env("REPORT_PREPARED_BY", "")
# Who may start a report run (it reads both servers for a while): no = only this computer;
# others can still follow it and download the files.
ALLOW_REMOTE = config._flag("REPORT_ALLOW_REMOTE", False)
# Examples of rows that differ (keys only) per table in the Excel file.
EXAMPLES_MAX = int(_env("REPORT_EXAMPLES_MAX", "10"))
# Test mode (testmode.py): check only this many of the smallest tables in each part, to try
# the whole report safely; every file is marked TEST. 0 = the normal, full report.
TEST_TABLES = max(0, int(_env("REPORT_TEST_TABLES", "0") or 0))
