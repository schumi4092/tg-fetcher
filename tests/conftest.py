"""Shared pytest fixtures.

Routes config.DB_PATH to a per-test temp file so tests never touch the real
tg_memory.db. Imports of `db` MUST happen after the fixture mutates DB_PATH
— do imports inside fixtures or test functions, not at module top-level.
"""

import os
import sys
import importlib
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def fresh_db(monkeypatch):
    """Yield a path to an empty SQLite file, with config.DB_PATH pointed at it.

    Reloads the `db` module so its DB_PATH-bound globals (none currently, but
    safer for future-proofing) re-read the patched value.
    """
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="tg_test_")
    os.close(fd)
    try:
        import config
        monkeypatch.setattr(config, "DB_PATH", path)
        import db
        importlib.reload(db)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        # Reload db once more so subsequent tests / modules see the original DB_PATH.
        import db
        importlib.reload(db)
