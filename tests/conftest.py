import pytest

from app.settings import settings
from app.storage import init_db


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.sqlite3")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    init_db()
