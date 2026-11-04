import pytest

from companion import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.apply_schema(c)
    try:
        yield c
    finally:
        c.close()
