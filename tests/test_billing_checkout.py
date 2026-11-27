"""Checkout-intent lifecycle: a failed provider call must not lock the user out.

Regression tests for the Codex review on PR #2: when checkout creation
fails at the provider, the pending BillingCheckout intent must be resolved
so the next attempt is not rejected with 409 for up to an hour — except
when the failure is ambiguous (the request may have reached the provider),
in which case the intent stays pending to avoid a duplicate checkout.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import configure_auth, install_auth_route_guard, register_auth_routes
from billing import (
    BillingProviderAmbiguousError,
    BillingProviderError,
    provider_request,
    register_billing,
)
from database.models import Base, BillingCheckout, User

ENV = {
    "BILLING_MODE": "test",
    "LEMON_API_KEY": "test-key",
    "LEMON_STORE_ID": "123",
    "LEMON_WEBHOOK_SECRET": "test-secret",
    "LEMON_PRO_MONTHLY_VARIANT_ID": "1",
    "PUBLIC_BASE_URL": "https://postenza.test",
}

SUCCESS_PAYLOAD = {
    "attributes": {
        "url": "https://lemonsqueezy.com/checkout/buy/abc123",
        "test_mode": True,
    }
}


class FakeResponse:
    def __init__(self, *, status=200, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class BillingCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, ENV)
        self.env_patch.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'billing-checkout.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        template_dir = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
        static_dir = Path(__file__).resolve().parents[1] / "dashboard" / "static"
        self.app = Flask(
            __name__,
            template_folder=str(template_dir),
            static_folder=str(static_dir),
        )
        self.app.config["TESTING"] = True
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="billing-checkout-test-secret",
            legacy_instagram_user_id="90000000000000001",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        register_billing(self.app)
        with self.Session.begin() as session:
            self.user = User(email="buyer@example.invalid", display_name="Buyer")
            session.add(self.user)
        self.client = self.app.test_client()

    def tearDown(self):
        factory = self.app.extensions["phase3a_auth"].session_factory
        if factory is not None:
            factory.kw["bind"].dispose()
        self.engine.dispose()
        self.tempdir.cleanup()
        self.env_patch.stop()

    def _login_as(self):
        with self.client.session_transaction() as session:
            session["user_id"] = str(self.user.id)
            session["_csrf_token"] = "csrf-test-token"

    def _checkout(self):
        return self.client.post(
            "/app/billing/checkout",
            data={"variant": "1", "csrf_token": "csrf-test-token"},
        )

    def _intents(self):
        with self.Session() as session:
            return session.scalars(
                select(BillingCheckout).where(BillingCheckout.user_id == self.user.id)
            ).all()

    def test_definitive_provider_failure_frees_intent_for_retry(self):
        self._login_as()
        with patch("billing.provider_request", side_effect=BillingProviderError("down")):
            response = self._checkout()
        self.assertEqual(response.status_code, 503)
        # The failed attempt left no pending intent behind.
        self.assertEqual(self._intents(), [])
        # A retry now succeeds instead of hitting 409 "already in progress".
        with patch("billing.provider_request", return_value=SUCCESS_PAYLOAD):
            response = self._checkout()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], SUCCESS_PAYLOAD["attributes"]["url"])
        intents = self._intents()
        self.assertEqual(len(intents), 1)
        self.assertIsNone(intents[0].consumed_by)

    def test_ambiguous_provider_failure_keeps_intent_and_blocks_retry(self):
        self._login_as()
        with patch("billing.provider_request", side_effect=BillingProviderAmbiguousError("maybe sent")):
            response = self._checkout()
        self.assertEqual(response.status_code, 503)
        # The checkout may exist at the provider: the intent must stay.
        self.assertEqual(len(self._intents()), 1)
        # And a retry is still refused while the intent is pending.
        with patch("billing.provider_request", side_effect=BillingProviderAmbiguousError("maybe sent")):
            response = self._checkout()
        self.assertEqual(response.status_code, 409)
        self.assertIn("already in progress", response.get_json()["error"])

    def test_environment_mismatch_keeps_intent(self):
        self._login_as()
        mismatched = {"attributes": {"url": "https://lemonsqueezy.com/checkout/buy/xyz", "test_mode": False}}
        with patch("billing.provider_request", return_value=mismatched):
            response = self._checkout()
        self.assertEqual(response.status_code, 503)
        # The provider did create a checkout, so the intent stays as the
        # record linking it to the user even though it cannot be completed.
        self.assertEqual(len(self._intents()), 1)

    def test_successful_checkout_keeps_intent_for_webhook(self):
        self._login_as()
        with patch("billing.provider_request", return_value=SUCCESS_PAYLOAD):
            response = self._checkout()
        self.assertEqual(response.status_code, 303)
        intents = self._intents()
        self.assertEqual(len(intents), 1)
        self.assertIsNone(intents[0].consumed_by)

    def test_provider_request_classifies_failures(self):
        cases = [
            # (requests.request behaviour, expected error class)
            (FakeResponse(status=400, payload={"data": {}}), BillingProviderError),
            (FakeResponse(status=404, payload={"data": {}}), BillingProviderError),
            (FakeResponse(status=500, payload={"data": {}}), BillingProviderAmbiguousError),
            (FakeResponse(status=502, payload={"data": {}}), BillingProviderAmbiguousError),
            (requests.ConnectTimeout("no connection"), BillingProviderError),
            (requests.ReadTimeout("no answer"), BillingProviderAmbiguousError),
            (requests.ConnectionError("dropped"), BillingProviderAmbiguousError),
            (FakeResponse(status=200, json_error=True), BillingProviderError),
        ]
        for behaviour, expected in cases:
            with self.subTest(behaviour=behaviour, expected=expected):
                if isinstance(behaviour, Exception):
                    side_effect = behaviour
                else:
                    side_effect = None
                with patch("billing.requests.request",
                           side_effect=side_effect, return_value=None if side_effect else behaviour):
                    if expected is BillingProviderAmbiguousError:
                        with self.assertRaises(BillingProviderAmbiguousError):
                            provider_request("POST", "checkouts", json={})
                    else:
                        with self.assertRaises(BillingProviderError) as caught:
                            provider_request("POST", "checkouts", json={})
                        self.assertNotIsInstance(caught.exception, BillingProviderAmbiguousError)

    def test_provider_request_returns_data_on_success(self):
        payload = {"data": {"id": "9", "type": "checkouts", "attributes": {"url": "https://example.test"}}}
        with patch("billing.requests.request", return_value=FakeResponse(status=201, payload=payload)):
            self.assertEqual(provider_request("POST", "checkouts", json={}), payload["data"])


if __name__ == "__main__":
    unittest.main()
