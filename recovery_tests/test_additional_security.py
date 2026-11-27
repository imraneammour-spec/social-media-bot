from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import select

import billing
from auth import validate_csrf_token
from database.billing import BillingLimitError, check_account_limit, effective_plan
from database.models import BillingSubscription, InstagramAccount, User


def test_unicode_csrf_is_rejected_without_crash(app):
    with app.test_request_context("/"):
        from flask import session
        session["_csrf_token"] = "expected-token"
        assert not validate_csrf_token("é-invalid")


def test_unicode_webhook_signature_is_rejected(client):
    assert client.post("/billing/webhook", data=b"{}", headers={"X-Signature":"é-invalid"}).status_code == 401


def test_missed_renewal_does_not_grant_perpetual_paid_access():
    subscription = BillingSubscription(plan="pro", status="active", test_mode=False,
        renews_at=datetime.now(timezone.utc)-timedelta(days=3))
    assert effective_plan(subscription) == "trial"


def test_disconnected_account_releases_slot(factory, user):
    with factory.begin() as db:
        db.add(InstagramAccount(user_id=user.id, instagram_user_id="disconnected-only", connection_status="disconnected"))
    with factory.begin() as db:
        check_account_limit(db, user.id)


def test_duplicate_checkout_is_blocked(client, monkeypatch):
    provider=Mock(return_value={"attributes":{"test_mode":True,"url":"https://store.lemonsqueezy.com/checkout/test"}})
    monkeypatch.setattr(billing,"provider_request",provider)
    data={"variant":"10","csrf_token":"test-csrf-token-with-enough-characters"}
    assert client.post("/app/billing/checkout",data=data).status_code == 303
    assert client.post("/app/billing/checkout",data=data).status_code == 409
    assert provider.call_count == 1


def test_invoice_history_is_scoped_and_safe(client, factory, user, monkeypatch):
    with factory.begin() as db:
        db.add(BillingSubscription(user_id=user.id,provider_id="100",plan="pro",status="active",test_mode=True,
            renews_at=datetime.now(timezone.utc)+timedelta(days=30)))
    invoices=[{"attributes":{"store_id":1,"test_mode":True,"subscription_id":100,"total_formatted":"Test amount", "status":"paid", "created_at":"2026-10-24", "urls":{"invoice_url":"https://app.lemonsqueezy.com/invoice/test"}}},
              {"attributes":{"store_id":1,"test_mode":True,"subscription_id":200,"total_formatted":"Other user's invoice", "urls":{}}}]
    provider=Mock(return_value=invoices)
    monkeypatch.setattr(billing,"provider_request",provider)
    response=client.get("/app/billing")
    assert response.status_code == 200
    assert b"Test amount" in response.data
    assert b"Other user's invoice" not in response.data
    assert provider.call_args.kwargs["params"]["filter[subscription_id]"] == "100"


def test_portal_uses_owned_subscription(client,factory,user,monkeypatch):
    with factory.begin() as db:
        db.add(BillingSubscription(user_id=user.id,provider_id="100",plan="pro",status="active",test_mode=True))
    provider=Mock(return_value={"attributes":{"store_id":1,"test_mode":True,"urls":{"customer_portal":"https://store.lemonsqueezy.com/billing/test"}}})
    monkeypatch.setattr(billing,"provider_request",provider)
    response=client.post("/app/billing/portal",data={"csrf_token":"test-csrf-token-with-enough-characters","subscription_id":"200"})
    assert response.status_code == 303
    provider.assert_called_once_with("GET","subscriptions/100")
