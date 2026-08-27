"""Read-only authenticated OAuth tenant dashboard routes."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, current_app, jsonify, make_response, redirect, render_template, request, session as browser_session, url_for
from sqlalchemy.exc import SQLAlchemyError

from auth import (
    action_reference_for_account,
    authentication_available,
    csrf_token,
    current_user_has_legacy_workspace_access,
    get_current_user,
    request_csrf_token,
    resolve_owned_account_action_reference,
    USER_SESSION_KEY,
    validate_csrf_token,
)
from database import tenant_dashboard as tenant_data


# Kept as a module-level boundary so server-side callers and focused tests use
# the exact same opaque lead-reference format.
lead_action_reference_for_prompt_request = tenant_data.lead_action_reference_for_prompt_request


ACCOUNT_REFERENCE_PURPOSE = "tenant-dashboard-select"
ACCOUNT_REFERENCE_ARGUMENT = "account"
ACCOUNT_UNAVAILABLE_MESSAGE = "Selected account is unavailable."
LEAD_UNAVAILABLE_MESSAGE = "Lead is unavailable."
LEAD_UPDATE_ERROR_MESSAGE = "Lead update could not be completed."
MAX_INTERNAL_NOTE_LENGTH = 2000
_DATABASE_UNAVAILABLE = object()


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
        if browser_session.get(USER_SESSION_KEY) is not None:
            return None, None, _workspace_unavailable()
        return None, None, _cache_control(redirect(url_for("login")))
    if current_user_has_legacy_workspace_access():
        return None, None, _cache_control(redirect(url_for("dashboard")))
    state = _auth_state()
    session_factory = getattr(state, "session_factory", None)
    if session_factory is None:
        return None, None, _workspace_unavailable()
    try:
        with session_factory() as session:
            accounts = tenant_data.list_tenant_accounts(
                session,
                user_id=user.id,
                legacy_instagram_user_id=getattr(state, "legacy_instagram_user_id", ""),
            )
    except SQLAlchemyError:
        return None, None, _workspace_unavailable()
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
        return _DATABASE_UNAVAILABLE
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
        return _DATABASE_UNAVAILABLE
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
            "activation_detail": (
                "Instagram engagement automation is not yet activated."
                if account.activation_label == "Activation not ready"
                else "Required connection checks are complete."
            ),
            "is_selected": account.instagram_account_id == selected.instagram_account_id,
            "can_reconnect": account.can_reconnect,
            "can_disconnect": account.can_disconnect,
        }
        for account in accounts
    ]


def _safe_account_unavailable():
    return _cache_control(jsonify(error=ACCOUNT_UNAVAILABLE_MESSAGE)), 404


def _workspace_unavailable():
    return _cache_control(make_response("Workspace temporarily unavailable.", 503))


def _safe_lead_unavailable():
    return _cache_control(jsonify(error=LEAD_UNAVAILABLE_MESSAGE)), 404


def _safe_lead_update_error():
    return _cache_control(jsonify(error=LEAD_UPDATE_ERROR_MESSAGE)), 400


def _render_page(template_name, *, user, accounts, selected, **context):
    account_payload = _account_payload(user, accounts, selected)
    active_pages = {
        "tenant_dashboard.app_overview": "overview",
        "tenant_dashboard.app_accounts": "accounts",
        "tenant_dashboard.app_leads": "leads",
        "tenant_dashboard.app_activity": "activity",
        "tenant_dashboard.app_followups": "followups",
    }
    return _cache_control(
        render_template(
            template_name,
            current_user=user,
            accounts=account_payload,
            selected_account=next(item for item in account_payload if item["is_selected"]),
            active_page=active_pages.get(request.endpoint, ""),
            csrf_token=csrf_token(),
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
        if selected is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        if selected is None:
            return _safe_account_unavailable()
        return view(user, accounts, selected, *args, **kwargs)

    return wrapped


def _query(selected, query, **kwargs):
    session_factory = getattr(_auth_state(), "session_factory", None)
    if session_factory is None:
        return _DATABASE_UNAVAILABLE
    try:
        with session_factory() as session:
            return query(session, instagram_account_id=selected.instagram_account_id, **kwargs)
    except SQLAlchemyError:
        return _DATABASE_UNAVAILABLE


def _lead_filters():
    return tenant_data.lead_filters(
        request.args.get("view"),
        request.args.get("stage"),
        request.args.get("q"),
        request.args.get("sort"),
        request.args.get("page"),
    )


def _lead_payload(user, selected, rows):
    payload = []
    for row in rows:
        payload.append(
            {
                "username": row.username,
                "requested_at": row.requested_at,
                "prompt_state": row.prompt_state,
                "base_stage": row.base_stage,
                "base_stage_label": row.base_stage_label,
                "archived": row.archived,
                "archived_label": row.archived_label,
                "internal_note": row.internal_note,
                "prompt_url": f"/p/o/{row.public_prompt_id}" if row.public_prompt_id else None,
                "status_action": tenant_data.lead_action_reference_for_prompt_request(
                    user.id,
                    selected.instagram_account_id,
                    row.prompt_request_id,
                    "lead-status",
                    secret_key=current_app.secret_key,
                ),
                "note_action": tenant_data.lead_action_reference_for_prompt_request(
                    user.id,
                    selected.instagram_account_id,
                    row.prompt_request_id,
                    "lead-note",
                    secret_key=current_app.secret_key,
                ),
                "archive_action": tenant_data.lead_action_reference_for_prompt_request(
                    user.id,
                    selected.instagram_account_id,
                    row.prompt_request_id,
                    "lead-archive",
                    secret_key=current_app.secret_key,
                ),
            }
        )
    return payload


def _lead_redirect():
    view, stage, search, sort, page = _lead_filters()
    return redirect(
        url_for(
            "tenant_dashboard.app_leads",
            account=request.args.get(ACCOUNT_REFERENCE_ARGUMENT),
            view=view,
            stage=stage,
            q=search or None,
            sort=sort,
            page=page,
        ),
        code=303,
    )


def _mutate_lead(user, selected, purpose, mutate):
    state = _auth_state()
    session_factory = getattr(state, "session_factory", None)
    if session_factory is None:
        return _safe_lead_unavailable()
    try:
        with session_factory.begin() as session:
            lead = tenant_data.resolve_tenant_lead_action_reference(
                session,
                user_id=user.id,
                instagram_account_id=selected.instagram_account_id,
                reference=request.form.get("lead_action"),
                purpose=purpose,
                secret_key=current_app.secret_key,
            )
            if lead is None:
                return _safe_lead_unavailable()
            mutate(lead)
    except SQLAlchemyError:
        return _safe_lead_unavailable()
    return _lead_redirect()


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
        if data is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return _render_page("app_overview.html", user=user, accounts=accounts, selected=selected, overview=data)

    @dashboard.get("/accounts")
    @_require_tenant
    def app_accounts(user, accounts, selected):
        return _render_page("app_accounts.html", user=user, accounts=accounts, selected=selected)

    @dashboard.get("/leads")
    @_require_tenant
    def app_leads(user, accounts, selected):
        view, stage, search, sort, page = _lead_filters()
        lead_page = _query(
            selected,
            tenant_data.leads,
            view=view,
            stage=stage,
            search=search,
            sort=sort,
            page=page,
        )
        if lead_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return _render_page(
            "app_leads.html",
            user=user,
            accounts=accounts,
            selected=selected,
            leads=_lead_payload(user, selected, lead_page.leads),
            lead_page=lead_page,
            selected_view=view,
            selected_stage=stage,
            selected_search=search,
            selected_sort=sort,
            lead_statuses=tenant_data.LEAD_STATUSES,
        )

    @dashboard.get("/activity")
    @_require_tenant
    def app_activity(user, accounts, selected):
        rows = _query(selected, tenant_data.activity)
        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return _render_page("app_activity.html", user=user, accounts=accounts, selected=selected, activity=rows)

    @dashboard.get("/followups")
    @_require_tenant
    def app_followups(user, accounts, selected):
        rows = _query(selected, tenant_data.followups)
        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return _render_page("app_followups.html", user=user, accounts=accounts, selected=selected, followups=rows)

    @dashboard.get("/api/accounts")
    @_require_tenant
    def app_api_accounts(user, accounts, selected):
        return jsonify(accounts=_account_payload(user, accounts, selected))

    @dashboard.get("/api/overview")
    @_require_tenant
    def app_api_overview(user, accounts, selected):
        data = _query(selected, tenant_data.overview)
        if data is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return jsonify(account=_account_payload(user, accounts, selected)[0] if len(accounts) == 1 else next(item for item in _account_payload(user, accounts, selected) if item["is_selected"]), overview=data)

    @dashboard.get("/api/leads")
    @_require_tenant
    def app_api_leads(user, accounts, selected):
        view, stage, search, sort, page = _lead_filters()
        lead_page = _query(
            selected,
            tenant_data.leads,
            view=view,
            stage=stage,
            search=search,
            sort=sort,
            page=page,
        )
        if lead_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return jsonify(
            leads=_lead_payload(user, selected, lead_page.leads),
            filters={"view": view, "stage": stage, "q": search, "sort": sort},
            pagination={
                "page": lead_page.page,
                "has_previous": lead_page.has_previous,
                "has_next": lead_page.has_next,
            },
        )

    @dashboard.get("/api/activity")
    @_require_tenant
    def app_api_activity(user, accounts, selected):
        rows = _query(selected, tenant_data.activity)
        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return jsonify(activity=rows)

    @dashboard.get("/api/followups")
    @_require_tenant
    def app_api_followups(user, accounts, selected):
        rows = _query(selected, tenant_data.followups)
        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()
        return jsonify(followups=rows)

    @dashboard.post("/leads/status")
    @_require_tenant
    def app_lead_status(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_lead_update_error()
        status = str(request.form.get("lead_status") or "").strip().lower()
        if status not in tenant_data.LEAD_STATUSES:
            return _safe_lead_update_error()
        return _mutate_lead(
            user,
            selected,
            "lead-status",
            lambda lead: setattr(lead, "lead_status", status),
        )

    @dashboard.post("/leads/note")
    @_require_tenant
    def app_lead_note(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_lead_update_error()
        note = str(request.form.get("internal_note") or "").strip()
        if len(note) > MAX_INTERNAL_NOTE_LENGTH:
            return _safe_lead_update_error()
        return _mutate_lead(
            user,
            selected,
            "lead-note",
            lambda lead: setattr(lead, "internal_note", note or None),
        )

    @dashboard.post("/leads/archive")
    @_require_tenant
    def app_lead_archive(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_lead_update_error()
        operation = str(request.form.get("operation") or "").strip().lower()
        if operation not in {"archive", "unarchive"}:
            return _safe_lead_update_error()
        archived_at = datetime.now(timezone.utc) if operation == "archive" else None
        return _mutate_lead(
            user,
            selected,
            "lead-archive",
            lambda lead: setattr(lead, "lead_archived_at", archived_at),
        )

    app.register_blueprint(dashboard)
