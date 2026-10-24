"""Real PostgreSQL integration checks. Only explicit disposable test databases."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from database.billing import BillingLimitError, consume_usage
from database.models import Base, BillingUsage, User


@pytest.fixture
def postgres_factory():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not provided; PostgreSQL integration was not executed.")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("Use an explicitly disposable database whose name ends in _test.")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_postgres_concurrent_quota_cannot_overspend(postgres_factory, monkeypatch):
    monkeypatch.setenv("BILLING_PLANS_JSON", '{"free":{"ai":5}}')
    with postgres_factory.begin() as db:
        user = User(email=f"quota-{uuid.uuid4()}@example.test")
        db.add(user); db.flush(); user_id = user.id

    def consume(_):
        try:
            with postgres_factory.begin() as db:
                consume_usage(db, user_id, "ai")
            return True
        except BillingLimitError:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        accepted = list(pool.map(consume, range(20)))
    assert sum(accepted) == 5
    with postgres_factory() as db:
        usage = db.scalar(select(BillingUsage).where(BillingUsage.user_id == user_id))
        assert usage.quantity == 5
