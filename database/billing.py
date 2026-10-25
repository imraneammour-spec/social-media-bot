"""Server-authoritative plans and transactional quotas. No card data is stored."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .models import BillingSubscription, BillingUsage, InstagramAccount, User


class BillingLimitError(ValueError):
    pass


DEFAULT_PLANS = {
    "free": {"name": "Free", "accounts": 1, "content": 30, "ai": 20},
    "pro": {"name": "Pro", "accounts": 3, "content": 300, "ai": 500},
    "agency": {"name": "Agency", "accounts": 10, "content": 1000, "ai": 2000},
    "legacy": {"name": "Existing workspace", "accounts": -1, "content": -1, "ai": -1},
}


def plans():
    result = {key: dict(value) for key, value in DEFAULT_PLANS.items()}
    overrides = json.loads(os.getenv("BILLING_PLANS_JSON", "{}"))
    for key, values in overrides.items():
        if key not in result or not isinstance(values, dict):
            raise ValueError("Invalid billing plan configuration.")
        for metric in ("accounts", "content", "ai"):
            if metric in values:
                value = values[metric]
                if type(value) is not int or value < -1:
                    raise ValueError("Invalid billing limit configuration.")
                result[key][metric] = value
    return result


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def effective_plan(subscription, now=None):
    now = now or datetime.now(timezone.utc)
    if subscription is None:
        return "free"
    if subscription.plan == "legacy" and subscription.status == "legacy":
        return "legacy"
    # Sandbox subscriptions must never unlock production features.
    production = os.getenv("APP_ENV", "").lower() in {"production", "prod"} or bool(os.getenv("RAILWAY_ENVIRONMENT_NAME"))
    if subscription.test_mode and production:
        return "free"
    renewal_current = bool(subscription.renews_at and utc(subscription.renews_at) + timedelta(hours=24) > now)
    if subscription.status in {"active", "on_trial"} and renewal_current and (not subscription.ends_at or utc(subscription.ends_at) > now):
        return subscription.plan if subscription.plan in {"pro", "agency"} else "free"
    if subscription.status == "cancelled" and subscription.ends_at and utc(subscription.ends_at) > now:
        return subscription.plan if subscription.plan in {"pro", "agency"} else "free"
    return "free"


def lock_user(session, user_id):
    user = session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise BillingLimitError("Account is unavailable.")
    return user


def check_account_limit(session, user_id):
    lock_user(session, user_id)
    plan = effective_plan(session.get(BillingSubscription, user_id))
    maximum = plans()[plan]["accounts"]
    used = session.scalar(select(func.count()).select_from(InstagramAccount).where(InstagramAccount.user_id == user_id, InstagramAccount.connection_status != "disconnected"))
    if maximum >= 0 and used >= maximum:
        raise BillingLimitError("Your connected-account limit has been reached. Manage your plan in Billing.")


def consume_usage(session, user_id, metric):
    lock_user(session, user_id)
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    maximum = plans()[effective_plan(session.get(BillingSubscription, user_id))][metric]
    usage = session.get(BillingUsage, (user_id, period, metric))
    if usage is None:
        usage = BillingUsage(user_id=user_id, period=period, metric=metric, quantity=0)
        session.add(usage)
    if maximum >= 0 and usage.quantity >= maximum:
        raise BillingLimitError("Your monthly usage limit has been reached. Manage your plan in Billing.")
    usage.quantity += 1
    session.flush()
    return period


def usage_snapshot(session, user_id):
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    plan = effective_plan(session.get(BillingSubscription, user_id))
    counts = {row.metric: row.quantity for row in session.scalars(select(BillingUsage).where(BillingUsage.user_id == user_id, BillingUsage.period == period))}
    counts["accounts"] = session.scalar(select(func.count()).select_from(InstagramAccount).where(InstagramAccount.user_id == user_id, InstagramAccount.connection_status != "disconnected"))
    return [{"metric": metric, "used": counts.get(metric, 0), "limit": maximum,
             "remaining": max(0, maximum - counts.get(metric, 0)) if maximum >= 0 else None}
            for metric, maximum in plans()[plan].items() if metric in {"accounts", "content", "ai"}]


def reserve_account_ai(factory, account_id):
    with factory.begin() as session:
        account = session.get(InstagramAccount, account_id)
        if account is None:
            raise BillingLimitError("Account is unavailable.")
        consume_usage(session, account.user_id, "ai")
