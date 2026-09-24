import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _reset_logging():
    """CLI tests attach handlers to pytest's captured stdout; drop them afterwards."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.WARNING)
