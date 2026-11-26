"""Real PostgreSQL integration checks. Only explicit disposable test databases."""
import os
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from database.billing import BillingLimitError, consume_usage
from database.models import Base, BillingSubscription, BillingUsage, User


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


@pytest.mark.parametrize("preload_subscription", [False, True])
def test_older_subscription_snapshot_cannot_restore_expired_plan(
    postgres_factory, monkeypatch, preload_subscription
):
    import billing

    monkeypatch.setenv("BILLING_MODE", "test")
    monkeypatch.setenv("LEMON_STORE_ID", "1")
    monkeypatch.setenv("LEMON_PRO_MONTHLY_VARIANT_ID", "10")
    identifier = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    newer_at = now - timedelta(minutes=1)
    with postgres_factory.begin() as db:
        user = User(email=f"subscription-race-{identifier}@example.test")
        db.add(user)
        db.flush()
        user_id = user.id
        db.add(BillingSubscription(
            user_id=user_id, provider_id=identifier, plan="pro", status="on_trial",
            test_mode=True, provider_updated_at=now - timedelta(minutes=3),
            renews_at=now + timedelta(days=30),
        ))

    owner_read = threading.Event()
    resume_older = threading.Event()
    older_thread = None
    original_lock = billing.lock_user

    def pause_before_lock(db, owner):
        if threading.get_ident() == older_thread:
            owner_read.set()
            assert resume_older.wait(10), "Newer synchronization did not finish."
        return original_lock(db, owner)

    monkeypatch.setattr(billing, "lock_user", pause_before_lock)

    def snapshot(status, updated):
        return {"type": "subscriptions", "id": identifier, "attributes": {
            "store_id": 1, "test_mode": True, "variant_id": 10,
            "status": status, "updated_at": updated.isoformat(),
            "renews_at": (now + timedelta(days=30)).isoformat(), "ends_at": None,
        }}

    def apply_older():
        nonlocal older_thread
        older_thread = threading.get_ident()
        with postgres_factory.begin() as db:
            # Callers may already have loaded this row into SQLAlchemy's identity map.
            cached = db.get(BillingSubscription, user_id) if preload_subscription else None
            billing.synchronize(db, identifier, snapshot("active", now - timedelta(minutes=2)))
            if cached is not None:
                assert cached.status == "expired"

    with ThreadPoolExecutor(max_workers=1) as pool:
        older = pool.submit(apply_older)
        try:
            assert owner_read.wait(10), "Older synchronization did not reach owner lookup."
            with postgres_factory.begin() as db:
                billing.synchronize(db, identifier, snapshot("expired", newer_at))
        finally:
            resume_older.set()
        older.result(timeout=10)

    with postgres_factory() as db:
        subscription = db.get(BillingSubscription, user_id)
        assert subscription.status == "expired"
        assert subscription.provider_updated_at == newer_at
