import pytest

from options_earnings.db.connection import open_memory


@pytest.fixture
def conn():
    c = open_memory()
    try:
        yield c
    finally:
        c.close()
