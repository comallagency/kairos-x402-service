import os
import tempfile

# Must run before any `app.*` import (pytest loads conftest.py first, ahead
# of collecting/importing test modules in this directory) - db.py does
# `from app.config import DB_PATH` at import time, so setting the env var
# here, before that import ever happens, is the only way to redirect it.
# Found 2026-10-09: with no override, DB_PATH defaulted to the same
# ./data/requests.db the live app writes to - test runs on this VPS were
# writing real rows (payer=0xtestpayer, user_agent=testclient) straight
# into production's own request log.
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="x402_test_db_")
os.environ.setdefault("DB_PATH", str(_TEST_DB_DIR.name + "/requests.db"))
