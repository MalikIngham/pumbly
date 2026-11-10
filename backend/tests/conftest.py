import pytest


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    '''Keeps tests away from the real database.'''
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'test.db'))
