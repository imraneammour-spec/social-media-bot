"""Credits top-up: plan allowance first, credits second, never silent.

Covers database/billing.py (consume_ai_usage / add_credits /
credits_balance / credits_alert / refund_ai_usage /
deduct_credits_for_refund) and billing.py
(credit checkout route, order_created and order_refunded webhooks,
 /app/billing display):
- the monthly AI allowance is consumed before any credit;
- when allowance AND credits are both gone the use is refused with a
  clear message (never silent);
- a purchase increases the balance, and a replayed order never
  credits twice;
- a refund deducts automatically, never below zero, and a replayed
  refund never deducts twice;
- credits never expire while the account is active.
"""
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import configure_auth, install_auth_route_guard, register_auth_routes
from billing import BillingProviderError, register_billing
from database.billing import (
    CREDIT_EXPIRY_DAYS,
    BillingLimitError,
    add_credits,
    consume_ai_usage,
    consume_usage,
    credits_alert,
    credits_balance,
    credits_snapshot,
    deduct_credits_for_refund,
    refund_ai_usage,
)
from database.models import (
    Base,
    BillingCheckout,
    BillingCreditLot,
    BillingSubscription,
    BillingUsage,
    User,
)


def _days_ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


class CreditsCoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'credits.db'}"
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _add_user(self, *, trial_started_at=None, email="user@example.invalid"):
        with self.Session.begin() as session:
            user = User(email=email, trial_started_at=trial_started_at)
            session.add(user)
            session.flush()
            return user.id

    def _add_legacy(self, user_id):
        with self.Session.begin() as session:
            session.add(BillingSubscription(
                user_id=user_id, plan="legacy", status="legacy", test_mode=False))

    def _ai_used(self, user_id):
        with self.Session() as session:
            row = session.get(BillingUsage, (user_id, datetime.now(timezone.utc).strftime("%Y-%m"), "ai"))
            return row.quantity if row else 0

    def test_plan_quota_consumed_before_credits(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_500")
        with self.Session.begin() as session:
            sources = [consume_ai_usage(session, user_id)["source"] for _ in range(301)]
        self.assertEqual(sources[:300], ["plan"] * 300)
        self.assertEqual(sources[300], "credits")
        # The plan row stops at its 300/mo limit; the 301st use came from credits.
        self.assertEqual(self._ai_used(user_id), 300)
        with self.Session() as session:
            self.assertEqual(credits_balance(session, user_id), 499)

    def test_both_exhausted_raises_clear_error_never_silent(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            for _ in range(300):  # trial AI allowance
                consume_ai_usage(session, user_id)
        with self.Session.begin() as session:
            with self.assertRaises(BillingLimitError) as caught:
                consume_ai_usage(session, user_id)
        self.assertIn("both used up", str(caught.exception))
        self.assertIn("Buy credits", str(caught.exception))
        self.assertEqual(self._ai_used(user_id), 300)

    def test_purchase_increases_balance_and_duplicate_order_is_idempotent(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_1200", provider_order_id="ord-1")
            self.assertEqual(credits_balance(session, user_id), 1200)
            add_credits(session, user_id, "credits_500", provider_order_id="ord-2")
            self.assertEqual(credits_balance(session, user_id), 1700)
            # A replayed order webhook must not credit twice.
            lot = add_credits(session, user_id, "credits_1200", provider_order_id="ord-1")
            self.assertEqual(credits_balance(session, user_id), 1700)
            self.assertEqual(lot.credits_total, 1200)
        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(BillingCreditLot)).all()), 2)

    def test_credits_never_expire_while_account_active(self):
        # Decision: CREDIT_EXPIRY_DAYS is None -- no expiry (see
        # database/billing.py). A lot bought 400 days ago still spends.
        self.assertIsNone(CREDIT_EXPIRY_DAYS)
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            lot = add_credits(session, user_id, "credits_500")
            self.assertIsNone(lot.expires_at)
            lot.created_at = _days_ago(400)
        with self.Session.begin() as session:
            for _ in range(300):
                consume_ai_usage(session, user_id)
            self.assertEqual(consume_ai_usage(session, user_id)["source"], "credits")
            self.assertEqual(credits_balance(session, user_id), 499)

    def test_refund_restores_the_source_it_took_from(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_500")
            plan_use = {"user_id": user_id, **consume_ai_usage(session, user_id)}
            self.assertEqual(plan_use["source"], "plan")
            refund_ai_usage(session, plan_use)
        self.assertEqual(self._ai_used(user_id), 0)
        with self.Session.begin() as session:
            for _ in range(300):
                consume_ai_usage(session, user_id)
            credit_use = {"user_id": user_id, **consume_ai_usage(session, user_id)}
            self.assertEqual(credit_use["source"], "credits")
            self.assertEqual(credits_balance(session, user_id), 499)
            refund_ai_usage(session, credit_use)
            self.assertEqual(credits_balance(session, user_id), 500)
        # Read after commit: the plan row is untouched by a credit refund.
        self.assertEqual(self._ai_used(user_id), 300)

    def test_full_refund_deducts_the_whole_pack_automatically(self):
        # Decision 2026-11-28: refund deduction is AUTOMATIC.
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_1200", provider_order_id="ord-1")
            self.assertEqual(credits_balance(session, user_id), 1200)
            deducted = deduct_credits_for_refund(session, user_id, 1200, provider_order_id="ord-1")
        self.assertEqual(deducted, 1200)
        with self.Session() as session:
            self.assertEqual(credits_balance(session, user_id), 0)

    def test_refund_never_drives_balance_below_zero(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            lot = add_credits(session, user_id, "credits_500", provider_order_id="ord-1")
            lot.credits_remaining = 10  # most of the pack already spent
            deducted = deduct_credits_for_refund(session, user_id, 500, provider_order_id="ord-1")
        self.assertEqual(deducted, 10)
        with self.Session() as session:
            self.assertEqual(credits_balance(session, user_id), 0)
            self.assertEqual(session.get(BillingCreditLot, lot.id).credits_remaining, 0)

    def test_refund_takes_the_refunded_order_lot_first(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            first = add_credits(session, user_id, "credits_500", provider_order_id="ord-1")
            second = add_credits(session, user_id, "credits_500", provider_order_id="ord-2")
            first.created_at = _days_ago(2)
            second.created_at = _days_ago(1)
            # Spend one credit: plan allowance (300) is exhausted first,
            # then the oldest lot (ord-1) is consumed.
            for _ in range(300):
                consume_ai_usage(session, user_id)
            consume_ai_usage(session, user_id)
            self.assertEqual(credits_balance(session, user_id), 999)
            # Refund the NEWER order: its own lot is emptied first,
            # the older lot keeps its 499.
            deducted = deduct_credits_for_refund(session, user_id, 500, provider_order_id="ord-2")
        self.assertEqual(deducted, 500)
        with self.Session() as session:
            self.assertEqual(session.get(BillingCreditLot, first.id).credits_remaining, 499)
            self.assertEqual(session.get(BillingCreditLot, second.id).credits_remaining, 0)
            self.assertEqual(credits_balance(session, user_id), 499)

    def test_refund_of_zero_or_unknown_amount_deducts_nothing(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_500", provider_order_id="ord-1")
            self.assertEqual(deduct_credits_for_refund(session, user_id, 0), 0)
            self.assertEqual(deduct_credits_for_refund(session, user_id, -50), 0)
            self.assertEqual(credits_balance(session, user_id), 500)

    def test_legacy_unlimited_plan_never_touches_credits(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        self._add_legacy(user_id)
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_500")
            sources = [consume_ai_usage(session, user_id)["source"] for _ in range(5)]
        self.assertEqual(sources, ["plan"] * 5)
        with self.Session() as session:
            self.assertEqual(credits_balance(session, user_id), 500)

    def test_credits_do_not_extend_content_or_accounts(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_500")
            for _ in range(30):  # trial content allowance
                consume_usage(session, user_id, "content")
            with self.assertRaises(BillingLimitError):
                consume_usage(session, user_id, "content")
        with self.Session() as session:
            self.assertEqual(credits_balance(session, user_id), 500)

    def test_expired_trial_can_still_spend_paid_credits(self):
        # Paid credits are paid uses: an expired trial stays blocked from
        # plan features, but credits it bought still spend on AI.
        user_id = self._add_user(trial_started_at=_days_ago(61))
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_500")
            self.assertEqual(consume_ai_usage(session, user_id)["source"], "credits")

    def test_oldest_lot_is_consumed_first(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            first = add_credits(session, user_id, "credits_500", provider_order_id="ord-1")
            second = add_credits(session, user_id, "credits_500", provider_order_id="ord-2")
            first.created_at = _days_ago(2)
            second.created_at = _days_ago(1)
            for _ in range(300):
                consume_ai_usage(session, user_id)
            consume_ai_usage(session, user_id)
        with self.Session() as session:
            self.assertEqual(session.get(BillingCreditLot, first.id).credits_remaining, 499)
            self.assertEqual(session.get(BillingCreditLot, second.id).credits_remaining, 500)

    def test_credits_alert_warns_at_80_percent_and_exhausted_at_zero(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session() as session:
            self.assertIsNone(credits_alert(session, user_id))
        with self.Session.begin() as session:
            for _ in range(239):
                consume_ai_usage(session, user_id)
        with self.Session() as session:
            self.assertIsNone(credits_alert(session, user_id))
        with self.Session.begin() as session:
            consume_ai_usage(session, user_id)  # 240 / 300 = 80%
        with self.Session() as session:
            self.assertEqual(credits_alert(session, user_id), "warning")
        with self.Session.begin() as session:
            for _ in range(60):
                consume_ai_usage(session, user_id)
        with self.Session() as session:
            self.assertEqual(credits_alert(session, user_id), "exhausted")
        # With credits left, a used-up allowance is a warning, not exhaustion.
        with self.Session.begin() as session:
            add_credits(session, user_id, "credits_500")
        with self.Session() as session:
            self.assertEqual(credits_alert(session, user_id), "warning")
            snapshot = credits_snapshot(session, user_id)
            self.assertEqual(snapshot["balance"], 500)
            self.assertEqual(snapshot["total_purchased"], 500)
            self.assertEqual(snapshot["total_used"], 0)


ENV = {
    "BILLING_MODE": "test",
    "LEMON_API_KEY": "test-key",
    "LEMON_STORE_ID": "1",
    "LEMON_WEBHOOK_SECRET": "test-secret",
    "LEMON_PRO_MONTHLY_VARIANT_ID": "10",
    "LEMON_CREDITS_500_VARIANT_ID": "50",
    "LEMON_CREDITS_1200_VARIANT_ID": "51",
    "LEMON_CREDITS_2500_VARIANT_ID": "52",
    "PUBLIC_BASE_URL": "https://postenza.test",
}

CHECKOUT_SUCCESS = {
    "attributes": {
        "url": "https://lemonsqueezy.com/checkout/buy/credits-abc",
        "test_mode": True,
    }
}


def _order_payload(*, order_id="9001", variant=50, status="paid", **extra_attributes):
    return {
        "type": "orders",
        "id": order_id,
        "attributes": {
            "store_id": 1,
            "test_mode": True,
            "status": status,
            "order_number": int(order_id),
            "first_order_item": {"variant_id": variant},
            **extra_attributes,
        },
    }


class CreditsBillingHttpTest(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, ENV)
        self.env_patch.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'billing-credits.db'}"
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
            secret_key="billing-credits-test-secret",
            legacy_instagram_user_id="90000000000000001",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        register_billing(self.app)
        with self.Session.begin() as session:
            self.user = User(
                email="buyer@example.invalid", display_name="Buyer",
                trial_started_at=datetime.now(timezone.utc),
            )
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

    def _balance(self):
        with self.Session() as session:
            return credits_balance(session, self.user.id)

    def _intent(self, *, variant="50"):
        with self.Session.begin() as session:
            row = BillingCheckout(user_id=self.user.id, variant_id=variant, test_mode=True)
            session.add(row)
            session.flush()
            return row

    def _send_order(self, row, *, event="order_created", data=None, signature=True):
        raw = json.dumps({"meta": {"event_name": event, "custom_data": {
            "checkout_id": str(row.id), "user_id": str(self.user.id),
        }}, "data": data or _order_payload()}).encode()
        signed = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        return self.client.post("/billing/webhook", data=raw,
                                content_type="application/json",
                                headers={"X-Signature": signed if signature else "invalid"})

    def test_unknown_credit_variant_checkout_is_refused(self):
        self._login_as()
        response = self.client.post("/app/billing/credits/checkout",
                                    data={"variant": "999", "csrf_token": "csrf-test-token"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("not available for purchase yet", response.get_json()["error"])

    def test_credit_checkout_success_redirects_to_provider(self):
        self._login_as()
        with patch("billing.provider_request", return_value=CHECKOUT_SUCCESS):
            response = self.client.post("/app/billing/credits/checkout",
                                        data={"variant": "50", "csrf_token": "csrf-test-token"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], CHECKOUT_SUCCESS["attributes"]["url"])
        with self.Session() as session:
            intents = session.scalars(select(BillingCheckout)).all()
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0].variant_id, "50")
            self.assertIsNone(intents[0].consumed_by)

    def test_definitive_credit_checkout_failure_frees_intent(self):
        self._login_as()
        with patch("billing.provider_request", side_effect=BillingProviderError("down")):
            self.assertEqual(self.client.post("/app/billing/credits/checkout",
                             data={"variant": "50", "csrf_token": "csrf-test-token"}).status_code, 503)
        with self.Session() as session:
            self.assertEqual(session.scalars(select(BillingCheckout)).all(), [])
        with patch("billing.provider_request", return_value=CHECKOUT_SUCCESS):
            self.assertEqual(self.client.post("/app/billing/credits/checkout",
                             data={"variant": "50", "csrf_token": "csrf-test-token"}).status_code, 303)

    def test_order_webhook_grants_credits_exactly_once(self):
        row = self._intent()
        with patch("billing.provider_request", return_value=_order_payload()):
            response = self._send_order(row)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "processed")
        self.assertEqual(self._balance(), 500)
        # Replaying the same signed webhook must not double-grant.
        with patch("billing.provider_request", return_value=_order_payload()):
            self.assertEqual(self._send_order(row).json["status"], "duplicate")
        self.assertEqual(self._balance(), 500)
        with self.Session() as session:
            self.assertEqual(session.get(BillingCheckout, row.id).consumed_by, "9001")

    def test_forged_order_webhook_grants_nothing(self):
        row = self._intent()
        with patch("billing.provider_request", return_value=_order_payload()) as provider:
            response = self._send_order(row, signature=False)
        self.assertEqual(response.status_code, 401)
        provider.assert_not_called()
        self.assertEqual(self._balance(), 0)

    def test_order_webhook_wrong_variant_never_grants(self):
        row = self._intent()
        with patch("billing.provider_request", return_value=_order_payload(variant=999)):
            response = self._send_order(row)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self._balance(), 0)

    def test_unpaid_order_grants_nothing(self):
        row = self._intent()
        with patch("billing.provider_request", return_value=_order_payload(status="pending")):
            response = self._send_order(row)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ignored")
        self.assertEqual(self._balance(), 0)

    def _grant_credits(self, row):
        with patch("billing.provider_request", return_value=_order_payload()):
            response = self._send_order(row)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "processed")
        self.assertEqual(self._balance(), 500)

    def test_refunded_order_webhook_deducts_credits_automatically(self):
        # Decision 2026-11-28: refund deduction is AUTOMATIC.
        row = self._intent()
        self._grant_credits(row)
        with patch("billing.provider_request", return_value=_order_payload(status="refunded")):
            response = self._send_order(row, event="order_refunded",
                                        data=_order_payload(status="refunded"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "processed")
        self.assertEqual(response.json["credits_deducted"], 500)
        self.assertEqual(self._balance(), 0)
        # Replaying the same signed refund must not deduct twice.
        with patch("billing.provider_request", return_value=_order_payload(status="refunded")):
            self.assertEqual(self._send_order(row, event="order_refunded",
                                              data=_order_payload(status="refunded")).json["status"],
                             "duplicate")
        self.assertEqual(self._balance(), 0)

    def test_refund_never_drives_balance_below_zero_via_webhook(self):
        row = self._intent()
        self._grant_credits(row)
        with self.Session.begin() as session:
            for _ in range(300):  # trial allowance
                consume_ai_usage(session, self.user.id)
            consume_ai_usage(session, self.user.id)  # 1 credit spent
        self.assertEqual(self._balance(), 499)
        with patch("billing.provider_request", return_value=_order_payload(status="refunded")):
            response = self._send_order(row, event="order_refunded",
                                        data=_order_payload(status="refunded"))
        self.assertEqual(response.json["status"], "processed")
        self.assertEqual(response.json["credits_deducted"], 499)
        self.assertEqual(self._balance(), 0)

    def test_refunded_unknown_order_is_ignored_not_an_error(self):
        row = self._intent()
        with patch("billing.provider_request", return_value=_order_payload(status="refunded")):
            response = self._send_order(row, event="order_refunded",
                                        data=_order_payload(status="refunded"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ignored")
        self.assertEqual(self._balance(), 0)

    def test_unrefunded_order_refund_event_is_ignored(self):
        row = self._intent()
        self._grant_credits(row)
        with patch("billing.provider_request", return_value=_order_payload(status="paid")):
            response = self._send_order(row, event="order_refunded",
                                        data=_order_payload(status="paid"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ignored")
        self.assertEqual(self._balance(), 500)

    def test_partial_refund_deducts_proportional_share_when_amount_known(self):
        row = self._intent()
        self._grant_credits(row)
        refunded = _order_payload(status="partially_refunded", total=500, refunded_amount=250)
        with patch("billing.provider_request", return_value=refunded):
            response = self._send_order(row, event="order_refunded", data=refunded)
        self.assertEqual(response.json["status"], "processed")
        self.assertEqual(response.json["credits_deducted"], 250)
        self.assertEqual(self._balance(), 250)

    def test_partial_refund_without_amount_deducts_whole_pack(self):
        # Lemon's order object carries no refunded amount: the approved
        # fallback (2026-11-28) deducts the whole pack, never a guess.
        row = self._intent()
        self._grant_credits(row)
        refunded = _order_payload(status="partially_refunded")
        with patch("billing.provider_request", return_value=refunded):
            response = self._send_order(row, event="order_refunded", data=refunded)
        self.assertEqual(response.json["status"], "processed")
        self.assertEqual(response.json["credits_deducted"], 500)
        self.assertEqual(self._balance(), 0)

    def test_dashboard_shows_balance_warning_and_exhaustion(self):
        self._login_as()
        response = self.client.get("/app/billing")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Credits", response.data)
        self.assertIn(b"Buy 500 credits", response.data)
        self.assertIn(b"never expire", response.data)
        with self.Session.begin() as session:
            for _ in range(240):
                consume_ai_usage(session, self.user.id)
        response = self.client.get("/app/billing")
        self.assertIn(b"80% of your monthly AI allowance", response.data)
        with self.Session.begin() as session:
            for _ in range(60):
                consume_ai_usage(session, self.user.id)
        response = self.client.get("/app/billing")
        self.assertIn(b"both used up", response.data)
        with self.Session.begin() as session:
            add_credits(session, self.user.id, "credits_500")
        response = self.client.get("/app/billing")
        self.assertIn(b"now draw on your credits", response.data)
        self.assertNotIn(b"both used up", response.data)


if __name__ == "__main__":
    unittest.main()
