import pytest

from app.store import Store


@pytest.fixture(scope="session")
def store() -> Store:
    return Store()
