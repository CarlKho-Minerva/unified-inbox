"""Shared fixtures for the unified-inbox unit tests.

Point the runner's module-level store at a throwaway directory *before* the
runner is imported anywhere, so route tests never read or write the live cache
under runtime/unified-inbox.
"""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault(
    "UNIFIED_INBOX_DATA_DIR", tempfile.mkdtemp(prefix="unified-inbox-test-")
)

from unified_inbox.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "data")
