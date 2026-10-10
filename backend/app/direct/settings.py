"""Settings of the direct report (backend/.env), all optional. Its files never mix with the
migration report's: own folder, own run file."""
from pathlib import Path

from .. import config

_env = config._env

# Where the direct reports are kept (one folder per run) and how many are kept.
REPORT_DIR = Path(_env("DIRECT_REPORT_DIR", config.BACKEND_DIR / ".cache" / "direct_reports"))
KEEP = max(1, int(_env("DIRECT_REPORT_KEEP", "20")))

# Test mode: only the N smallest mappings (by client records, from the catalogs) are graded;
# the files are marked TEST. 0 = every mapping.
TEST_MAPPINGS = max(0, int(_env("DIRECT_TEST_MAPPINGS", "0")))

TITLE = _env("DIRECT_REPORT_TITLE", "Direct Migration Check")
