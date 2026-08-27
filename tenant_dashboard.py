"""Read-only authenticated OAuth tenant dashboard routes."""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, current_app, jsonify, make_response, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from auth import (
    action_reference_for_account,
    authentication_available,
    current_user_has_legacy_workspace_access,
    get_current_user,
    resolve_owned_account_action_reference,
)
from database import tenant_dashboard as tenant_data


ACCOUNT_REFERENCE_PURPOSE = "tenant-dashboard-select"
ACCOUNT_REFERENCE_ARGUMENT = "account"
ACCOUNT_UNAVAILABLE_MESSAGE = "Selected account is unavailable."


def _auth_state():
    return current_app.extensions.get("phase3a_auth")


def _cache_control(response):
    response = make_response(response)
    response.headers["Cache-Control"] = "no-store"
    return response


def _tenant_context():
    if not authentication_available():
        return None, None, _cache_control(redirect(url_for("login")))
    user = get_current_user()
    if user is None:
        return None, None, _cache_control(redirect(url_for("login")))
    if current_user_has_legacy_workspace_access():
        return None, None, _cache_control(redirect(url_for("dashboard")))
    state = _auth_state()
    session_factory = getattr(state, "session_factory", None)
    if session_factory is None:
        return None, None, _cache_control(redirect(url_for("onboarding")))
    try:
        with session_factory() as session:
            accounts = tenant_data.list_tenant_accounts(
                session,
                user_id=user.id,
                legacy_instagram_user_id=getattr(state, "legacy_instagram_user_id", ""),
            )
    except SQLAlchemyError:
        return None, None, _cache_control(redirect(url_for("onboarding")))
    if not accounts:
        return None, None, _cache_control(redirect(url_for("onboarding")))
    return user, accounts, None


def _selected_account(user, accounts):
    supplied_reference = request.args.get(ACCOUNT_REFERENCE_ARGUMENT)
    if not supplied_reference:
        return accounts[0]
    state = _auth_state()
    session_factory = getattr(state, "session_factory", None)
    if session_factory is None:
        return None
    try:
        with session_factory() as session:
            account = resolve_owned_account_action_reference(
                session,
                user_id=user.id,
                reference=supplied_reference,
                purpose=ACCOUNT_REFERENCE_PURPOSE,
                secret_key=current_app.secret_key,
            )
    except SQLAlchemyError:
        return None
    if account is None:
        return None
    return next((item for item in accounts if item.instagram_account_id == account.id), None)


def _account_payload(user, accounts, selected):
    return [
        {
            "username": account.username,
            "selection_ref": action_reference_for_account(
                user.id,
                account.instagram_account_id,
                ACCOUNT_REFERENCE_PURPOSE,
                secret_key=current_app.secret_key,
            ),
            "connection_label": account.connection_label,
            "connection_detail": account.connection_detail,
            "activation_label": account.activation_label,
            "is_selected": account.instagram_account_id == selected.instagram_account_id,
            "can_reconnect": account.can_reconnect,
            "can_disconnect": account.can_disconnect,
        }
        for account in accounts
    ]


def _safe_account_unavailable():
    return _cache_control(jsonify(error=ACCOUNT_UNAVAILABLE_MESSAGE)), 404


def _render_page(template_name, *, user, accounts, selected, **context):
    return _cache_control(
        render_template(
            template_name,
            current_user=user,
            accounts=_account_payload(user, accounts, selected),
            selected_account=next(
                item for item in _account_payload(user, accounts, selected) if item["is_selected"]
            ),
            **context,
        )
    )


def _require_tenant(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user, accounts, response = _tenant_context()
        if response is not None:
            return response
        selected = _selected_account(user, accounts)
        if selected is None:
            return _safe_account_unavailable()
        return view(user, accounts, selected, *args, **kwargs)

    return wrapped


def _query(selected, query):
    session_factory = getattr(_auth_state(), "session_factory", None)
    if session_factory is None:
        return None
    try:
        with session_factory() as session:
            return query(session, instagram_account_id=selected.instagram_account_id)
    except SQLAlchemyError:
        return None


def register_tenant_dashboard(app) -> None:
    """Register additive read-only OAuth tenant routes under ``/app``."""

    dashboard = Blueprint("tenant_dashboard", __name__, url_prefix="/app")

    @dashboard.after_request
    def tenant_no_store(response):
        return _cache_control(response)

    @dashboard.get("")
    @_require_tenant
    def app_overview(user, accounts, selected):
        data = _query(selected, tenant_data.overview)
        if data is None:
            return _safe_account_unavailable()
        return _render_page("app_overview.html", user=user, accounts=accounts, selected=selected, overview=data)

    @dashboard.get("/accounts")
    @_require_tenant
    def app_accounts(user, accounts, selected):
        return _render_page("app_accounts.html", user=user, accounts=accounts, selected=selected)

    @dashboard.get("/leads")
    @_require_tenant
    def app_leads(user, accounts, selected):
        rows = _query(selected, tenant_data.leads)
        if rows is None:
            return _safe_account_unavailable()
        leads = [
            {
                "username": row.username,
                "requested_at": row.requested_at,
                "prompt_state": row.prompt_state,
                "prompt_url": f"/p/o/{row.public_prompt_id}" if row.public_prompt_id else None,
            }
            for row in rows
        ]
        return _render_page("app_leads.html", user=user, accounts=accounts, selected=selected, leads=leads)

    @dashboard.get("/activity")
    @_require_tenant
    def app_activity(user, accounts, selected):
        rows = _query(selected, tenant_data.activity)
        if rows is None:
            return _safe_account_unavailable()
        return _render_page("app_activity.html", user=user, accounts=accounts, selected=selected, activity=rows)

    @dashboard.get("/followups")
    @_require_tenant
    def app_followups(user, accounts, selected):
        rows = _query(selected, tenant_data.followups)
        if rows is None:
            return _safe_account_unavailable()
        return _render_page("app_followups.html", user=user, accounts=accounts, selected=selected, followups=rows)

    @dashboard.get("/api/accounts")
    @_require_tenant
    def app_api_accounts(user, accounts, selected):
        return jsonify(accounts=_account_payload(user, accounts, selected))

    @dashboard.get("/api/overview")
    @_require_tenant
    def app_api_overview(user, accounts, selected):
        data = _query(selected, tenant_data.overview)
        if data is None:
            return _safe_account_unavailable()
        return jsonify(account=_account_payload(user, accounts, selected)[0] if len(accounts) == 1 else next(item for item in _account_payload(user, accounts, selected) if item["is_selected"]), overview=data)

    @dashboard.get("/api/leads")
    @_require_tenant
    def app_api_leads(user, accounts, selected):
        rows = _query(selected, tenant_data.leads)
        if rows is None:
            return _safe_account_unavailable()
        return jsonify(leads=[
            {
                "username": row.username,
                "requested_at": row.requested_at,
                "prompt_state": row.prompt_state,
                "prompt_url": f"/p/o/{row.public_prompt_id}" if row.public_prompt_id else None,
            }
            for row in rows
        ])

    @dashboard.get("/api/activity")
    @_require_tenant
    def app_api_activity(user, accounts, selected):
        rows = _query(selected, tenant_data.activity)
        if rows is None:
            return _safe_account_unavailable()
        return jsonify(activity=rows)

    @dashboard.get("/api/followups")
    @_require_tenant
    def app_api_followups(user, accounts, selected):
        rows = _query(selected, tenant_data.followups)
        if rows is None:
            return _safe_account_unavailable()
        return jsonify(followups=rows)

    app.register_blueprint(dashboard)
