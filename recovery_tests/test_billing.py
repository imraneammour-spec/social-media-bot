import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import select

import billing
from database.billing import BillingLimitError, check_account_limit, consume_usage, effective_plan
from database.models import BillingCheckout, BillingSubscription, BillingUsage, BillingWebhook, InstagramAccount, User, UserOnboardingProfile


def snapshot(status="active", **updates):
    attributes = {"store_id": 1, "variant_id": 10, "test_mode": True, "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(), "renews_at": "2026-12-01T00:00:00Z", "ends_at": None}
    attributes.update(updates)
    return {"type": "subscriptions", "id": "100", "attributes": attributes}


def intent(factory, user):
    with factory.begin() as db:
        row = BillingCheckout(user_id=user.id, variant_id="10", test_mode=True)
        db.add(row)
        db.flush()
    return row


def send(client, row, user, *, event="subscription_created", data=None, signature=True):
    raw = json.dumps({"meta": {"event_name": event, "custom_data": {"checkout_id": str(row.id), "user_id": str(user.id)}}, "data": data or snapshot()}).encode()
    signed = hmac.new(b"test-only-signing-secret", raw, hashlib.sha256).hexdigest()
    return client.post("/billing/webhook", data=raw, content_type="application/json", headers={"X-Signature": signed if signature else "invalid"})


def test_dashboard_is_private_and_available_before_instagram_connection(client):
    response = client.get("/app/billing")
    assert response.status_code == 200
    assert b"Billing &amp; plans" in response.data
    assert b"Free" in response.data
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert response.headers["Cache-Control"] == "no-store"


def test_anonymous_billing_does_not_leak(app):
    response = app.test_client().get("/app/billing")
    assert response.status_code == 302
    assert "/login" in response.location


def test_csrf_and_unconfigured_checkout_fail_closed(client, monkeypatch):
    assert client.post("/app/billing/checkout", data={"variant": "10"}).status_code == 400
    monkeypatch.setenv("BILLING_MODE", "disabled")
    response = client.post("/app/billing/checkout", data={"variant": "10", "csrf_token": "test-csrf-token-with-enough-characters"})
    assert response.status_code == 503


def test_checkout_uses_server_variant_nonce_and_test_mode(client, monkeypatch, factory, user):
    provider = Mock(return_value={"attributes": {"test_mode": True, "url": "https://store.lemonsqueezy.com/checkout/abc"}})
    monkeypatch.setattr(billing, "provider_request", provider)
    response = client.post("/app/billing/checkout", data={"variant":"10", "csrf_token":"test-csrf-token-with-enough-characters", "user_id":"attacker", "price":"1"})
    assert response.status_code == 303
    payload = provider.call_args.kwargs["json"]["data"]
    assert payload["attributes"]["test_mode"] is True
    assert "custom_price" not in payload["attributes"]
    assert payload["attributes"]["checkout_data"]["custom"]["user_id"] == str(user.id)
    with factory() as db:
        assert db.scalar(select(BillingCheckout)).user_id == user.id
        assert db.get(BillingSubscription, user.id) is None


def test_unapproved_live_mode_is_disabled(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "live")
    monkeypatch.delenv("BILLING_LIVE_APPROVED", raising=False)
    assert billing.mode() == "disabled"


def test_forged_webhook_does_not_fetch_or_grant(client, factory, user, monkeypatch):
    provider = Mock()
    monkeypatch.setattr(billing, "provider_request", provider)
    response = send(client, intent(factory, user), user, signature=False)
    assert response.status_code == 401
    provider.assert_not_called()
    with factory() as db:
        assert db.get(BillingSubscription, user.id) is None


def test_verified_webhook_binds_nonce_and_is_idempotent(client, factory, user, monkeypatch):
    monkeypatch.setattr(billing, "provider_request", Mock(return_value=snapshot()))
    row = intent(factory, user)
    data = snapshot()
    assert send(client, row, user, data=data).status_code == 200
    assert send(client, row, user, data=data).json["status"] == "duplicate"
    with factory() as db:
        subscription = db.get(BillingSubscription, user.id)
        assert effective_plan(subscription) == "pro"
        assert db.get(BillingCheckout, row.id).consumed_by == "100"
        assert len(db.scalars(select(BillingWebhook)).all()) == 1


@pytest.mark.parametrize("updates", [{"store_id": 2}, {"test_mode": False}, {"variant_id": 999}, {"status": "fake"}])
def test_wrong_store_mode_variant_or_status_never_grants(updates, client, factory, user, monkeypatch):
    monkeypatch.setattr(billing, "provider_request", Mock(return_value=snapshot(**updates)))
    assert send(client, intent(factory, user), user).status_code == 422
    with factory() as db:
        assert db.get(BillingSubscription, user.id) is None


def test_cross_user_checkout_binding_rejected(client, factory, user, monkeypatch):
    with factory.begin() as db:
        other = User(email="other@example.test")
        db.add(other)
        db.flush()
    monkeypatch.setattr(billing, "provider_request", Mock(return_value=snapshot()))
    assert send(client, intent(factory, other), user).status_code == 422


@pytest.mark.parametrize("status", ["past_due", "unpaid", "expired", "paused"])
def test_nonpaying_statuses_fall_back_to_free(status, client, factory, user, monkeypatch):
    monkeypatch.setattr(billing, "provider_request", Mock(return_value=snapshot(status)))
    assert send(client, intent(factory, user), user).status_code == 200
    with factory() as db:
        assert effective_plan(db.get(BillingSubscription, user.id)) == "trial"


def test_cancelled_entitlement_ends_at_confirmed_date():
    subscription = BillingSubscription(plan="pro", status="cancelled", test_mode=False, ends_at=datetime.now(timezone.utc)+timedelta(days=1))
    assert effective_plan(subscription) == "pro"
    assert effective_plan(subscription, now=datetime.now(timezone.utc)+timedelta(days=2)) == "trial"


def test_sandbox_never_unlocks_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert effective_plan(BillingSubscription(plan="pro", status="active", test_mode=True)) == "trial"


def test_onboarding_preference_never_grants_paid_plan(factory, user):
    with factory.begin() as db:
        db.add(UserOnboardingProfile(user_id=user.id, selected_plan="agency"))
        assert effective_plan(db.get(BillingSubscription, user.id)) == "trial"


def test_usage_is_backend_enforced_and_rollback_safe(factory, user, monkeypatch):
    monkeypatch.setenv("BILLING_PLANS_JSON", '{"free":{"content":1}}')
    with factory.begin() as db:
        consume_usage(db, user.id, "content")
    with pytest.raises(BillingLimitError):
        with factory.begin() as db:
            consume_usage(db, user.id, "content")
    with factory() as db:
        assert db.scalar(select(BillingUsage)).quantity == 1


def test_account_limit_is_backend_enforced(factory, user):
    with factory.begin() as db:
        db.add(InstagramAccount(user_id=user.id, instagram_user_id="1234"))
    with factory.begin() as db, pytest.raises(BillingLimitError):
        check_account_limit(db, user.id)


def test_legacy_access_preserved(factory, user):
    with factory.begin() as db:
        subscription = BillingSubscription(user_id=user.id, plan="legacy", status="legacy", test_mode=False)
        db.add(subscription)
        db.flush()
        assert effective_plan(subscription) == "legacy"
        for _ in range(35):
            consume_usage(db, user.id, "content")


@pytest.mark.parametrize("url", ["https://evil.example/", "http://store.lemonsqueezy.com/", "https://lemonsqueezy.com.evil.example/", "https://user:secret@lemonsqueezy.com/"])
def test_provider_redirects_are_allowlisted(url):
    with pytest.raises(billing.BillingProviderError):
        billing.hosted_url(url)
