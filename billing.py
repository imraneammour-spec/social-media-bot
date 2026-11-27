"""Hosted Lemon Squeezy billing, defaulting to disabled; live charging needs approval."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests
from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from auth import csrf_token, get_current_user, login_required, request_csrf_token, validate_csrf_token
from database.billing import BillingLimitError, consume_usage, effective_plan, lock_user, plans, usage_snapshot, utc
from database.models import BillingCheckout, BillingSubscription, BillingUsage, BillingWebhook


class BillingProviderError(ValueError):
    pass


class BillingProviderAmbiguousError(BillingProviderError):
    """The provider request may have reached Lemon Squeezy.

    Raised for timeouts after the request was sent and for 5xx responses,
    where the checkout may or may not have been created server-side.
    Callers must NOT assume the provider-side operation did not happen
    (in particular, they must not free a pending checkout intent for
    immediate retry, or a duplicate checkout could be created).
    """


def mode():
    value = os.getenv("BILLING_MODE", "disabled")
    if value == "live" and os.getenv("BILLING_LIVE_APPROVED") != "true":
        return "disabled"
    return value if value in {"test", "live"} else "disabled"


def variants():
    result = {}
    for plan in ("pro", "agency"):
        for interval in ("monthly", "annual"):
            value = os.getenv(f"LEMON_{plan.upper()}_{interval.upper()}_VARIANT_ID", "")
            if value:
                if not value.isdigit() or value in result:
                    raise BillingProviderError("Invalid payment plan configuration.")
                result[value] = (plan, interval)
    return result


def configured():
    return bool(mode() != "disabled" and os.getenv("LEMON_API_KEY") and os.getenv("LEMON_STORE_ID") and os.getenv("LEMON_WEBHOOK_SECRET") and variants())


def provider_request(method, path, **kwargs):
    if not configured():
        raise BillingProviderError("Payments are not available yet.")
    try:
        response = requests.request(method, "https://api.lemonsqueezy.com/v1/" + path,
            headers={"Authorization": "Bearer " + os.environ["LEMON_API_KEY"],
                     "Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json"},
            timeout=(5, 20), **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or "data" not in payload:
            raise ValueError
        return payload["data"]
    except requests.HTTPError as exc:
        # The provider answered: a 4xx means it rejected the request, so no
        # checkout was created. A 5xx may have failed after creating it.
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and status < 500:
            raise BillingProviderError("The payment provider is temporarily unavailable. Please try again.") from None
        raise BillingProviderAmbiguousError("The payment provider is temporarily unavailable. Please try again.") from None
    except requests.ConnectTimeout:
        # The connection never opened, so the request was never sent.
        raise BillingProviderError("The payment provider is temporarily unavailable. Please try again.") from None
    except requests.Timeout:
        # Includes read timeouts: the request was sent, the answer is unknown.
        raise BillingProviderAmbiguousError("The payment provider is temporarily unavailable. Please try again.") from None
    except requests.RequestException:
        # A dropped connection mid-flight: the request may have been sent.
        raise BillingProviderAmbiguousError("The payment provider is temporarily unavailable. Please try again.") from None
    except ValueError:
        # The provider answered, but not with a usable checkout payload.
        raise BillingProviderError("The payment provider is temporarily unavailable. Please try again.") from None


def hosted_url(value):
    if not isinstance(value, str):
        raise BillingProviderError("Payment link unavailable.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or not (
        parsed.hostname == "lemonsqueezy.com" or (parsed.hostname or "").endswith(".lemonsqueezy.com")
    ):
        raise BillingProviderError("Payment link unavailable.")
    return value


def provider_attributes(data):
    attributes = data.get("attributes", {})
    if str(attributes.get("store_id")) != os.getenv("LEMON_STORE_ID") or type(attributes.get("test_mode")) is not bool or attributes["test_mode"] != (mode() == "test"):
        raise BillingProviderError("Payment environment mismatch.")
    return attributes


def date(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A timezone is required.")
    return parsed.astimezone(timezone.utc)


def synchronize(db, subscription_id, data, *, checkout_id=None, user_id=None):
    """Use a fresh API snapshot, never checkout redirects or unverified browser state."""
    if data.get("type") != "subscriptions" or str(data.get("id")) != subscription_id:
        raise BillingProviderError("Subscription mismatch.")
    attributes = provider_attributes(data)
    variant = str(attributes.get("variant_id"))
    if variant not in variants():
        raise BillingProviderError("Unrecognized subscription variant.")
    # Resolve ownership without caching mutable subscription state before the lock.
    existing_user_id = db.scalar(select(BillingSubscription.user_id).where(BillingSubscription.provider_id == subscription_id))
    checkout_key = None
    if existing_user_id is not None:
        user_id = existing_user_id
    else:
        checkout_key = uuid.UUID(str(checkout_id))
        checkout_owner = db.scalar(select(BillingCheckout.user_id).where(BillingCheckout.id == checkout_key))
        if checkout_owner is None or str(checkout_owner) != str(user_id):
            raise BillingProviderError("Checkout ownership could not be verified.")
        user_id = checkout_owner
    lock_user(db, user_id)
    if checkout_key is not None:
        checkout = db.get(BillingCheckout, checkout_key, populate_existing=True, with_for_update=True)
        if not checkout or str(checkout.user_id) != str(user_id) or checkout.variant_id != variant or checkout.test_mode != attributes["test_mode"] or checkout.consumed_by not in {None, subscription_id}:
            raise BillingProviderError("Checkout ownership could not be verified.")
        if utc(checkout.created_at) < datetime.now(timezone.utc) - timedelta(hours=48):
            raise BillingProviderError("Checkout expired.")
        checkout.consumed_by = subscription_id
        user_id = checkout.user_id
    # Refresh even if the caller previously loaded this row into its identity map.
    subscription = db.get(BillingSubscription, user_id, populate_existing=True, with_for_update=True)
    if subscription is None:
        subscription = BillingSubscription(user_id=user_id)
        db.add(subscription)
    if subscription.provider_id not in {None, subscription_id} and subscription.status != "expired":
        raise BillingProviderError("A different subscription is already associated with this account.")
    updated = date(attributes.get("updated_at"))
    if updated is None:
        raise BillingProviderError("Subscription timestamp missing.")
    if subscription.provider_updated_at and utc(subscription.provider_updated_at) > updated:
        return subscription
    status = attributes.get("status")
    if status not in {"active", "on_trial", "past_due", "unpaid", "cancelled", "expired", "paused"}:
        raise BillingProviderError("Unrecognized subscription status.")
    subscription.provider_id = subscription_id
    subscription.plan, subscription.interval = variants()[variant]
    subscription.status = status
    subscription.test_mode = attributes["test_mode"]
    subscription.renews_at = date(attributes.get("renews_at") or attributes.get("trial_ends_at"))
    subscription.ends_at = date(attributes.get("ends_at"))
    subscription.provider_updated_at = updated
    db.flush()
    return subscription


def reconcile_subscriptions(factory):
    if not configured():
        return 0
    with factory() as db:
        identifiers = list(db.scalars(select(BillingSubscription.provider_id).where(
            BillingSubscription.provider_id.is_not(None), BillingSubscription.test_mode == (mode() == "test"))))
    count = 0
    for identifier in identifiers:
        try:
            data = provider_request("GET", "subscriptions/" + identifier)
            with factory.begin() as db:
                synchronize(db, identifier, data)
            count += 1
        except (BillingProviderError, SQLAlchemyError, ValueError):
            # Never include identifiers, payloads, or provider URLs in logs.
            import logging
            logging.getLogger(__name__).warning("billing_reconciliation_failed")
    return count


def register_billing(app):
    routes = Blueprint("billing", __name__)

    def factory():
        return current_app.extensions["phase3a_auth"].session_factory

    @routes.after_request
    def private(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @routes.get("/app/billing")
    @login_required
    def dashboard():
        user = get_current_user()
        error = None
        invoices = []
        with factory()() as db:
            subscription = db.get(BillingSubscription, user.id)
            usage = usage_snapshot(db, user.id)
            current_plan = plans()[effective_plan(subscription)]
        if configured() and subscription and subscription.provider_id:
            try:
                data = provider_request("GET", "subscription-invoices", params={"filter[subscription_id]": subscription.provider_id, "page[size]": 25})
                for invoice in data:
                    attributes = provider_attributes(invoice)
                    if str(attributes.get("subscription_id")) != subscription.provider_id:
                        continue
                    link = attributes.get("urls", {}).get("invoice_url")
                    invoices.append({"created": attributes.get("created_at"), "total": attributes.get("total_formatted"),
                                     "status": attributes.get("status"), "url": hosted_url(link) if link else None})
            except BillingProviderError as exc:
                error = str(exc)
        return render_template("billing.html", current_user=user, subscription=subscription,
            current_plan=current_plan, usage=usage, invoices=invoices, error=error,
            csrf_token=csrf_token(), plans=plans(), variants=variants(), available=configured(), mode=mode())

    @routes.post("/app/billing/checkout")
    @login_required
    def checkout():
        if not validate_csrf_token(request_csrf_token()):
            return jsonify(error="Invalid form token."), 400
        variant = request.form.get("variant")
        if variant not in variants() or not configured():
            return jsonify(error="This plan is not available for purchase yet."), 503
        user = get_current_user()
        with factory().begin() as db:
            lock_user(db, user.id)
            subscription = db.get(BillingSubscription, user.id)
            if subscription and subscription.provider_id and subscription.status != "expired":
                return jsonify(error="Manage your existing subscription in the payment portal."), 409
            pending = db.scalar(select(BillingCheckout).where(BillingCheckout.user_id == user.id,
                BillingCheckout.consumed_by.is_(None), BillingCheckout.created_at > datetime.now(timezone.utc) - timedelta(hours=1)))
            if pending:
                return jsonify(error="A checkout is already in progress. Complete it or wait for it to expire before trying again."), 409
            intent = BillingCheckout(user_id=user.id, variant_id=variant, test_mode=mode() == "test")
            db.add(intent)
            db.flush()
            intent_id = str(intent.id)
        origin = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        attributes = {"test_mode": mode() == "test", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "checkout_data": {"email": user.email, "custom": {"user_id": str(user.id), "checkout_id": intent_id}},
            "product_options": {"enabled_variants": [int(variant)], "redirect_url": origin + "/app/billing"}}
        payload = {"data": {"type": "checkouts", "attributes": attributes, "relationships": {
            "store": {"data": {"type": "stores", "id": os.environ["LEMON_STORE_ID"]}},
            "variant": {"data": {"type": "variants", "id": variant}}}}}
        try:
            result = provider_request("POST", "checkouts", json=payload)
        except BillingProviderAmbiguousError:
            # The checkout may have been created server-side; keep the intent
            # pending so the user cannot immediately create a duplicate.
            raise
        except BillingProviderError:
            # Definitive failure: the provider never created a checkout, so
            # the intent would otherwise block retries with 409 for an hour.
            # Remove it (only while still unconsumed) so the user can retry.
            with factory().begin() as db:
                stale = db.get(BillingCheckout, uuid.UUID(intent_id))
                if stale is not None and stale.consumed_by is None:
                    db.delete(stale)
            raise
        # NOTE: the test_mode mismatch check below deliberately stays outside
        # the try above: a mismatched response means the provider DID create
        # a checkout, so this is not a "nothing happened" failure. The intent
        # stays as the record linking that checkout to the user, and a retry
        # would fail the same way until the configuration is fixed.
        if result.get("attributes", {}).get("test_mode") != (mode() == "test"):
            raise BillingProviderError("Checkout environment mismatch.")
        return redirect(hosted_url(result["attributes"]["url"]), 303)

    @routes.post("/app/billing/portal")
    @login_required
    def portal():
        if not validate_csrf_token(request_csrf_token()):
            return jsonify(error="Invalid form token."), 400
        user = get_current_user()
        with factory()() as db:
            subscription = db.get(BillingSubscription, user.id)
            subscription_id = subscription.provider_id if subscription else None
        if not subscription_id:
            return jsonify(error="No paid subscription is associated with this account."), 409
        data = provider_request("GET", "subscriptions/" + subscription_id)
        attributes = provider_attributes(data)
        return redirect(hosted_url(attributes.get("urls", {}).get("customer_portal")), 303)

    @routes.post("/billing/webhook")
    def webhook():
        if not configured():
            return jsonify(error="Billing is not configured."), 503
        raw = request.get_data(cache=True)
        if len(raw) > 65536:
            return jsonify(error="Webhook too large."), 413
        expected = hmac.new(os.environ["LEMON_WEBHOOK_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode("ascii"), request.headers.get("X-Signature", "").encode("utf-8")):
            return jsonify(error="Invalid signature."), 401
        try:
            payload = json.loads(raw)
            event = payload["meta"]["event_name"]
            data = payload["data"]
            if not isinstance(event, str):
                raise ValueError
            if not event.startswith("subscription_"):
                return jsonify(status="ignored")
            attributes = provider_attributes(data)
            subscription_id = str(attributes.get("subscription_id")) if data.get("type") == "subscription-invoices" else str(data["id"])
            if not subscription_id.isdigit():
                raise ValueError
            digest = hashlib.sha256(raw).hexdigest()
            with factory()() as db:
                if db.get(BillingWebhook, digest):
                    return jsonify(status="duplicate")
            fresh = provider_request("GET", "subscriptions/" + subscription_id)
            custom = payload["meta"].get("custom_data", {})
            with factory().begin() as db:
                synchronize(db, subscription_id, fresh, checkout_id=custom.get("checkout_id"), user_id=custom.get("user_id"))
                db.add(BillingWebhook(digest=digest, event_name=event[:64]))
            return jsonify(status="processed")
        except IntegrityError:
            with factory()() as db:
                if db.get(BillingWebhook, digest):
                    return jsonify(status="duplicate")
            return jsonify(error="Subscription update conflict."), 409
        except (ValueError, KeyError, TypeError, AttributeError):
            return jsonify(error="Webhook could not be synchronized."), 422

    @routes.errorhandler(BillingProviderError)
    def provider_error(error):
        return jsonify(error=str(error)), 503

    @routes.errorhandler(SQLAlchemyError)
    def database_error(error):
        current_app.logger.error("billing_database_unavailable")
        return jsonify(error="Billing is temporarily unavailable."), 503

    @app.errorhandler(BillingLimitError)
    def limit_error(error):
        return jsonify(error=str(error)), 403

    @app.before_request
    def ai_quota():
        if request.method != "POST" or request.path not in {"/api/generate-content", "/api/content-planner", "/app/leads/analyze"}:
            return None
        user = get_current_user()
        if user:
            with factory().begin() as db:
                period = consume_usage(db, user.id, "ai")
            g.billing_ai_reservation = (user.id, period)

    @app.after_request
    def refund_failed_ai(response):
        reservation = getattr(g, "billing_ai_reservation", None)
        if reservation and response.status_code >= 400:
            try:
                with factory().begin() as db:
                    user_id, period = reservation
                    lock_user(db, user_id)
                    usage = db.get(BillingUsage, (user_id, period, "ai"))
                    if usage:
                        usage.quantity = max(0, usage.quantity - 1)
            except SQLAlchemyError:
                current_app.logger.error("billing_usage_refund_failed")
        return response

    app.register_blueprint(routes)
