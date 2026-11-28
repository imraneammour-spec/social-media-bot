"""Server-authoritative plans and transactional quotas. No card data is stored."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .models import BillingCreditLot, BillingSubscription, BillingUsage, InstagramAccount, User


class BillingLimitError(ValueError):
    pass


# Approved pricing (2026-11-27). The USD/MAD amounts below are display data
# only: the amounts actually charged are defined by the Lemon Squeezy
# variants in billing.variants(). "content" is scheduled posts per month
# (-1 means unlimited image scheduling); "ai" is AI uses per month.
TRIAL_DAYS = 60
TRIAL_REMINDER_DAYS = (45, 55)  # Days into the trial. See trial_reminder_due().
PAID_PLANS = ("starter", "pro", "business")

DEFAULT_PLANS = {
    "trial": {"name": "Trial", "accounts": 1, "content": 30, "ai": 300, "seats": 1,
              "analytics": False, "trial_days": TRIAL_DAYS,
              "price_monthly_usd": 0, "price_annual_usd": 0,
              "price_monthly_mad": 0, "price_annual_mad": 0},
    "starter": {"name": "Starter", "accounts": 1, "content": -1, "ai": 200, "seats": 1,
                "analytics": False, "trial_days": 0,
                "price_monthly_usd": 9, "price_annual_usd": 7,
                "price_monthly_mad": 90, "price_annual_mad": 70},
    "pro": {"name": "Pro", "accounts": 3, "content": -1, "ai": 1000, "seats": 2,
            "analytics": True, "trial_days": 0,
            "price_monthly_usd": 19, "price_annual_usd": 15,
            "price_monthly_mad": 190, "price_annual_mad": 150},
    "business": {"name": "Business", "accounts": 10, "content": -1, "ai": 5000, "seats": 5,
                 "analytics": True, "trial_days": 0,
                 "price_monthly_usd": 39, "price_annual_usd": 31,
                 "price_monthly_mad": 390, "price_annual_mad": 310},
    # No-subscription workspaces whose 60-day trial has expired (only returned
    # when a caller supplies trial_started_at; see effective_plan).
    "trial_expired": {"name": "Trial expired", "accounts": 0, "content": 0, "ai": 0, "seats": 0,
                      "analytics": False, "trial_days": 0,
                      "price_monthly_usd": 0, "price_annual_usd": 0,
                      "price_monthly_mad": 0, "price_annual_mad": 0},
    "legacy": {"name": "Existing workspace", "accounts": -1, "content": -1, "ai": -1, "seats": -1,
               "analytics": True, "trial_days": 0,
               "price_monthly_usd": None, "price_annual_usd": None,
               "price_monthly_mad": None, "price_annual_mad": None},
}

# Pre-approved-rename aliases: stored subscription plans, onboarding
# preferences and BILLING_PLANS_JSON overrides may still say "free"/"agency".
_LEGACY_PLAN_KEYS = {"free": "trial", "agency": "business"}


# --- Credits (top-up for AI uses, 2026-11-27) --------------------------------
# Credits supplement a plan, they never replace it: 1 credit = 1 AI use
# (image-prompt extraction / AI use). A credit is consumed ONLY after the
# plan's monthly AI allowance is exhausted, in this order: plan allowance
# first, then credits. Credits never buy accounts or scheduled posts;
# those limits stay plan-only.
#
# Approved packs: $5 = 500 credits, $10 = 1200 credits, $19 = 2500 credits.
# The USD amounts here are display data only: the amounts actually charged
# are defined by the Lemon Squeezy one-time variants in billing.py
# (credit_variants()), exactly like the subscription variants.
CREDIT_PACKS = {
    "credits_500": {"credits": 500, "price_usd": 5, "price_mad": 50},
    "credits_1200": {"credits": 1200, "price_usd": 10, "price_mad": 100},
    "credits_2500": {"credits": 2500, "price_usd": 19, "price_mad": 190},
}

# Credits expiry decision (2026-11-27): credits NEVER expire while the
# account is active (expires_at stays NULL on every lot). A 12-month
# expiry was considered and NOT chosen: it is not technically easier --
# it would need per-lot expiry filtering, partial-expiry edge cases
# (a lot half-used when it expires) and a background expiry job, while
# no-expiry needs none of that. The nullable expires_at column on
# BillingCreditLot is kept so a future expiry policy can be switched on
# per lot without a schema change.
CREDIT_EXPIRY_DAYS = None

# Warn on the billing page once this share of the monthly AI allowance
# is used (see credits_alert()).
AI_USAGE_WARNING_FRACTION = 0.8


def credit_packs():
    """Return a copy of the approved credit packs."""
    return {key: dict(value) for key, value in CREDIT_PACKS.items()}


def normalize_plan(plan):
    """Map a stored/legacy plan key to its current canonical key."""
    if not isinstance(plan, str):
        return "trial"
    return _LEGACY_PLAN_KEYS.get(plan, plan)


def plans():
    result = {key: dict(value) for key, value in DEFAULT_PLANS.items()}
    overrides = json.loads(os.getenv("BILLING_PLANS_JSON", "{}"))
    for key, values in overrides.items():
        key = _LEGACY_PLAN_KEYS.get(key, key)
        if key not in result or not isinstance(values, dict):
            raise ValueError("Invalid billing plan configuration.")
        for metric in ("accounts", "content", "ai", "seats"):
            if metric in values:
                value = values[metric]
                if type(value) is not int or value < -1:
                    raise ValueError("Invalid billing limit configuration.")
                result[key][metric] = value
    return result


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def trial_days_elapsed(started_at, now=None):
    """Whole days elapsed since the trial started, or None if unknown."""
    if started_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (utc(now) - utc(started_at)).days)


def trial_days_remaining(started_at, now=None):
    """Days of the 60-day trial left, or None if the start is unknown."""
    elapsed = trial_days_elapsed(started_at, now)
    if elapsed is None:
        return None
    return max(0, TRIAL_DAYS - elapsed)


def trial_expired(started_at, now=None):
    """True once 60 days have passed since the trial started."""
    elapsed = trial_days_elapsed(started_at, now)
    return elapsed is not None and elapsed >= TRIAL_DAYS


def trial_reminder_due(started_at, now=None):
    """Return 45 or 55 when today is that day into the trial, else None.

    Exact-day schedule helper. The actual sender, send_trial_reminders()
    below, uses elapsed >= day (catch-up) so a reminder is not lost when
    the sender was down on the exact day; this helper stays exact-day
    for callers that need the strict schedule.
    """
    elapsed = trial_days_elapsed(started_at, now)
    if elapsed in TRIAL_REMINDER_DAYS:
        return elapsed
    return None


def _trial_plan(trial_started_at, now):
    return "trial_expired" if trial_expired(trial_started_at, now) else "trial"


def effective_plan(subscription, now=None, trial_started_at=None):
    """Resolve the plan that gates limits right now.

    Trial support: a workspace with no subscription is on the 60-day free
    trial ("trial": 1 account, 30 scheduled posts/month, 300 AI uses/month,
    no card). The trial start is User.trial_started_at, set at
    registration; workspaces created before it existed are stamped with
    the first run of the new code (see resolve_trial_start), never
    backdated to created_at. Once 60 days have passed the workspace
    resolves to "trial_expired" (0 accounts, 0 content, 0 AI uses) until
    it buys a paid plan; its data is kept, only the paid features stop.
    Lemon Squeezy trials are separate: they arrive as status "on_trial"
    on a paid variant, with renews_at set from the provider's
    trial_ends_at, and keep that variant's limits.
    """
    now = now or datetime.now(timezone.utc)
    if subscription is None:
        return _trial_plan(trial_started_at, now)
    if subscription.plan == "legacy" and subscription.status == "legacy":
        return "legacy"
    # Sandbox subscriptions must never unlock production features.
    production = os.getenv("APP_ENV", "").lower() in {"production", "prod"} or bool(os.getenv("RAILWAY_ENVIRONMENT_NAME"))
    if subscription.test_mode and production:
        return _trial_plan(trial_started_at, now)
    renewal_current = bool(subscription.renews_at and utc(subscription.renews_at) + timedelta(hours=24) > now)
    if subscription.status in {"active", "on_trial"} and renewal_current and (not subscription.ends_at or utc(subscription.ends_at) > now):
        plan = normalize_plan(subscription.plan)
        if plan in PAID_PLANS:
            return plan
        return _trial_plan(trial_started_at, now)
    if subscription.status == "cancelled" and subscription.ends_at and utc(subscription.ends_at) > now:
        plan = normalize_plan(subscription.plan)
        if plan in PAID_PLANS:
            return plan
        return _trial_plan(trial_started_at, now)
    return _trial_plan(trial_started_at, now)


def resolve_trial_start(session, user_id, now=None, *, stamp=True):
    """Return the user's trial start, stamping legacy users on first use.

    New users get trial_started_at at registration. A workspace created
    before the trial was enforced has NULL here: its trial is taken to
    start at the first run of this code (the deploy date), NOT at the
    account's created_at -- backdating would expire those workspaces the
    moment enforcement lands. With stamp=True the start is written back
    (the caller owns the commit); with stamp=False (read-only callers
    such as usage_snapshot) the in-memory "now" is returned without a
    write, so a read never invents a durable start.
    """
    now = now or datetime.now(timezone.utc)
    user = session.get(User, user_id)
    if user is None:
        return None
    if user.trial_started_at is None:
        if stamp:
            user.trial_started_at = utc(now)
            session.flush()
        return utc(now)
    return user.trial_started_at


def send_trial_reminders(session_factory, *, sender=None, now=None):
    """Send the day-45 / day-55 trial reminders once each, via `sender`.

    `sender(user, days_remaining)` must return True only when the
    reminder was actually delivered. Runs over every workspace whose
    effective plan is still "trial"; paid, legacy and already-expired
    workspaces are skipped, and no user data is touched. A milestone is
    due once the trial is that many days in (elapsed >= day, so a
    reminder is not lost if the sender was down on the exact day) and is
    marked sent in the user's notification_preferences, so a later run
    never resends it. With sender=None (or when the sender reports
    failure) due reminders are counted under "due_unsent" and nothing is
    marked sent. Returns {"checked", "sent", "due_unsent", "skipped"}.
    """
    now = now or datetime.now(timezone.utc)
    checked = sent = due_unsent = skipped = 0
    with session_factory.begin() as session:
        for user in session.scalars(select(User)):
            checked += 1
            subscription = session.get(BillingSubscription, user.id)
            trial_started_at = resolve_trial_start(session, user.id, now=now)
            if effective_plan(subscription, now=now, trial_started_at=trial_started_at) != "trial":
                skipped += 1
                continue
            elapsed = trial_days_elapsed(trial_started_at, now)
            if elapsed is None:
                skipped += 1
                continue
            preferences = dict(user.notification_preferences or {})
            for day in TRIAL_REMINDER_DAYS:
                if elapsed < day:
                    continue
                key = f"trial_reminder_{day}_sent_at"
                if preferences.get(key):
                    continue
                days_remaining = max(0, TRIAL_DAYS - elapsed)
                delivered = sender(user, days_remaining) if sender is not None else False
                if delivered:
                    preferences[key] = now.isoformat()
                    user.notification_preferences = preferences
                    sent += 1
                else:
                    due_unsent += 1
    return {"checked": checked, "sent": sent, "due_unsent": due_unsent, "skipped": skipped}


def lock_user(session, user_id):
    user = session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise BillingLimitError("Account is unavailable.")
    return user


def check_account_limit(session, user_id):
    lock_user(session, user_id)
    plan = effective_plan(
        session.get(BillingSubscription, user_id),
        trial_started_at=resolve_trial_start(session, user_id),
    )
    maximum = plans()[plan]["accounts"]
    used = session.scalar(select(func.count()).select_from(InstagramAccount).where(InstagramAccount.user_id == user_id, InstagramAccount.connection_status != "disconnected"))
    if maximum >= 0 and used >= maximum:
        raise BillingLimitError("Your connected-account limit has been reached. Manage your plan in Billing.")


def credits_balance(session, user_id, now=None):
    """Usable credit balance: sum of remaining credits on unexpired lots.

    With CREDIT_EXPIRY_DAYS = None no lot ever expires, so this is the
    sum of every lot's remainder; lots with an expires_at in the past
    (only possible if a future policy sets one) are excluded.
    """
    now = now or datetime.now(timezone.utc)
    lots = session.scalars(
        select(BillingCreditLot).where(BillingCreditLot.user_id == user_id)
    ).all()
    return sum(
        lot.credits_remaining
        for lot in lots
        if lot.credits_remaining > 0
        and (lot.expires_at is None or utc(lot.expires_at) > now)
    )


def credits_snapshot(session, user_id, now=None):
    """Dashboard view of credits: balance, totals and per-lot detail."""
    now = now or datetime.now(timezone.utc)
    lots = session.scalars(
        select(BillingCreditLot)
        .where(BillingCreditLot.user_id == user_id)
        .order_by(BillingCreditLot.created_at, BillingCreditLot.id)
    ).all()
    usable = [
        lot for lot in lots
        if lot.expires_at is None or utc(lot.expires_at) > now
    ]
    return {
        "balance": sum(lot.credits_remaining for lot in usable if lot.credits_remaining > 0),
        "total_purchased": sum(lot.credits_total for lot in lots),
        "total_used": sum(lot.credits_total - lot.credits_remaining for lot in lots),
        "lots": [
            {"pack_key": lot.pack_key, "total": lot.credits_total,
             "remaining": lot.credits_remaining,
             "purchased_at": lot.created_at,
             "expires_at": lot.expires_at}
            for lot in lots
        ],
    }


def add_credits(session, user_id, pack_key, *, provider_order_id=None):
    """Credit one purchased pack to a user. Returns the lot.

    Idempotent on provider_order_id: a replayed / duplicate order
    webhook returns the existing lot instead of crediting twice.
    Unknown packs and non-positive amounts are rejected, never stored.
    """
    lock_user(session, user_id)
    if provider_order_id is not None:
        existing = session.scalar(
            select(BillingCreditLot).where(
                BillingCreditLot.provider_order_id == str(provider_order_id)
            )
        )
        if existing is not None:
            if str(existing.user_id) != str(user_id) or existing.pack_key != pack_key:
                raise BillingLimitError("Credit purchase could not be verified.")
            return existing
    pack = CREDIT_PACKS.get(pack_key)
    if pack is None:
        raise ValueError("Unknown credit pack.")
    expires_at = None
    if CREDIT_EXPIRY_DAYS is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=CREDIT_EXPIRY_DAYS)
    lot = BillingCreditLot(
        user_id=user_id, pack_key=pack_key,
        credits_total=pack["credits"], credits_remaining=pack["credits"],
        provider_order_id=str(provider_order_id) if provider_order_id is not None else None,
        expires_at=expires_at,
    )
    session.add(lot)
    session.flush()
    return lot


def _consume_one_credit(session, user_id, now=None):
    """Take 1 credit from the oldest usable lot (FIFO). Returns the lot id, or None."""
    now = now or datetime.now(timezone.utc)
    lots = session.scalars(
        select(BillingCreditLot)
        .where(BillingCreditLot.user_id == user_id, BillingCreditLot.credits_remaining > 0)
        .order_by(BillingCreditLot.created_at, BillingCreditLot.id)
        .with_for_update()
    ).all()
    for lot in lots:
        if lot.expires_at is not None and utc(lot.expires_at) <= now:
            continue
        lot.credits_remaining -= 1
        session.flush()
        return lot.id
    return None


def consume_ai_usage(session, user_id, now=None):
    """Consume one AI use: plan allowance first, credits second.

    Returns {"period", "source", "lot_id"} where source is "plan" or
    "credits". Raises BillingLimitError when the monthly allowance AND
    the credit balance are both exhausted -- with a message that says
    so and points at buying credits or upgrading, never silently.
    Unlimited plans ("legacy", ai = -1) never touch credits.
    """
    lock_user(session, user_id)
    now = now or datetime.now(timezone.utc)
    period = now.strftime("%Y-%m")
    maximum = plans()[effective_plan(
        session.get(BillingSubscription, user_id),
        trial_started_at=resolve_trial_start(session, user_id),
    )]["ai"]
    usage = session.get(BillingUsage, (user_id, period, "ai"))
    if usage is None:
        usage = BillingUsage(user_id=user_id, period=period, metric="ai", quantity=0)
        session.add(usage)
    if maximum < 0 or usage.quantity < maximum:
        usage.quantity += 1
        session.flush()
        return {"period": period, "source": "plan", "lot_id": None}
    lot_id = _consume_one_credit(session, user_id, now=now)
    if lot_id is not None:
        return {"period": period, "source": "credits", "lot_id": lot_id}
    raise BillingLimitError(
        "Your monthly AI allowance and your credits are both used up. "
        "Buy credits or upgrade your plan in Billing to continue."
    )


def refund_ai_usage(session, reservation):
    """Undo one consume_ai_usage() reservation (failed request rollback).

    Restores exactly what was taken: a "plan" use decrements the monthly
    BillingUsage row, a "credits" use returns 1 credit to the same lot
    (capped at the lot total, so a refund can never over-credit).
    """
    if not reservation:
        return
    user_id, period = reservation["user_id"], reservation["period"]
    lock_user(session, user_id)
    if reservation.get("source") == "credits" and reservation.get("lot_id"):
        lot = session.get(BillingCreditLot, reservation["lot_id"])
        if lot is not None and str(lot.user_id) == str(user_id):
            lot.credits_remaining = min(lot.credits_total, lot.credits_remaining + 1)
    else:
        usage = session.get(BillingUsage, (user_id, period, "ai"))
        if usage:
            usage.quantity = max(0, usage.quantity - 1)
    session.flush()


def deduct_credits_for_refund(session, user_id, credits, *, provider_order_id=None):
    """Automatically claw back credits when a credit purchase is refunded.

    Decision 2026-11-28: a refund deducts AUTOMATICALLY (no manual step).
    Deducts ``credits`` from the user's lots, starting with the lot of
    the refunded order, then the oldest remaining lots (FIFO). The
    deduction is capped at the current balance, so it can never drive
    the balance below zero; returns the number of credits actually
    deducted. Returns 0 when there is nothing to deduct (no lots, or
    ``credits`` <= 0). Idempotency is the caller's job: the webhook
    handler records the event digest in the same transaction (see
    billing.webhook), so a replayed order_refunded cannot deduct twice.
    """
    if not credits or credits <= 0:
        return 0
    lock_user(session, user_id)
    now = datetime.now(timezone.utc)
    lots = session.scalars(
        select(BillingCreditLot)
        .where(BillingCreditLot.user_id == user_id, BillingCreditLot.credits_remaining > 0)
        .order_by(BillingCreditLot.created_at, BillingCreditLot.id)
        .with_for_update()
    ).all()
    # The refunded order's own lot goes first: a refund claws back the
    # purchase it refunds before touching other packs.
    if provider_order_id is not None:
        refunded = [lot for lot in lots if str(lot.provider_order_id) == str(provider_order_id)]
        others = [lot for lot in lots if str(lot.provider_order_id) != str(provider_order_id)]
        lots = refunded + others
    remaining = int(credits)
    deducted = 0
    for lot in lots:
        if remaining <= 0:
            break
        if lot.expires_at is not None and utc(lot.expires_at) <= now:
            continue
        take = min(remaining, lot.credits_remaining)
        lot.credits_remaining -= take
        remaining -= take
        deducted += take
    session.flush()
    return deducted


def credits_alert(session, user_id):
    """Billing-page AI alert: None, "warning" (80% of allowance used) or "exhausted".

    "warning" fires once 80% of the monthly AI allowance is used, even
    when credits remain (the user is burning through the plan). "exhausted"
    fires only when the allowance AND the credit balance are both at zero
    -- when credits remain, the allowance being used up is not an
    exhaustion, it just means usage now draws on credits.
    """
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    plan = effective_plan(
        session.get(BillingSubscription, user_id),
        trial_started_at=resolve_trial_start(session, user_id, stamp=False),
    )
    maximum = plans()[plan]["ai"]
    if maximum is None or maximum < 0:
        return None
    used = session.get(BillingUsage, (user_id, period, "ai"))
    used_quantity = used.quantity if used else 0
    if used_quantity >= maximum and credits_balance(session, user_id) <= 0:
        return "exhausted"
    if maximum > 0 and used_quantity / maximum >= AI_USAGE_WARNING_FRACTION:
        return "warning"
    return None


def consume_usage(session, user_id, metric):
    lock_user(session, user_id)
    if metric == "ai":
        # AI goes through the plan-then-credits order; the period is
        # returned for the historical single-value callers.
        return consume_ai_usage(session, user_id)["period"]
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    maximum = plans()[effective_plan(
        session.get(BillingSubscription, user_id),
        trial_started_at=resolve_trial_start(session, user_id),
    )][metric]
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
    # Read-only caller: resolve without stamping, so viewing the billing
    # page never invents a durable trial start (see resolve_trial_start).
    plan = effective_plan(
        session.get(BillingSubscription, user_id),
        trial_started_at=resolve_trial_start(session, user_id, stamp=False),
    )
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
        # AI use: plan allowance first, credits second (consume_ai_usage).
        consume_ai_usage(session, account.user_id)
