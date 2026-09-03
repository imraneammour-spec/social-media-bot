"""Authenticated OAuth tenant dashboard routes."""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps

from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session as browser_session,
    url_for,
)
from sqlalchemy import select
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
from database.lead_intelligence import LeadIntelligence, lead_analysis_context
from database.oauth_deployment import oauth_deployment_readiness
from database.content import (
    CONTENT_TYPES,
    MAX_CAPTION_LENGTH,
    create_content_draft,
    delete_content_draft,
    schedule_content_draft,
    transition_content_status,
    unschedule_content_draft,
    update_content_draft,
)
from database.models import InstagramAutomationConfig, InstagramContentItem
from database.tenant_automation import (
    DEFAULT_FOLLOWUP_DELAY_MINUTES,
    DEFAULT_FOLLOWUP_ENABLED,
    SMART_REPLY_TONES,
    normalize_followup_delay_minutes,
    normalize_smart_reply_max_per_hour,
    normalize_smart_reply_tone,
    normalize_trigger_keywords,
)


lead_action_reference_for_prompt_request = (
    tenant_data.lead_action_reference_for_prompt_request
)


ACCOUNT_REFERENCE_PURPOSE = "tenant-dashboard-select"
ACCOUNT_REFERENCE_ARGUMENT = "account"

ACCOUNT_UNAVAILABLE_MESSAGE = "Selected account is unavailable."
LEAD_UNAVAILABLE_MESSAGE = "Lead is unavailable."
LEAD_UPDATE_ERROR_MESSAGE = "Lead update could not be completed."
AUTOMATION_UPDATE_ERROR_MESSAGE = "Automation update could not be completed."
CONTENT_UPDATE_ERROR_MESSAGE = "Draft update could not be completed."

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
        return (
            None,
            None,
            _cache_control(
                redirect(
                    url_for("login")
                )
            ),
        )

    user = get_current_user()

    if user is None:
        if browser_session.get(USER_SESSION_KEY) is not None:
            return (
                None,
                None,
                _workspace_unavailable(),
            )

        return (
            None,
            None,
            _cache_control(
                redirect(
                    url_for("login")
                )
            ),
        )

    if current_user_has_legacy_workspace_access():
        return (
            None,
            None,
            _cache_control(
                redirect(
                    url_for("dashboard")
                )
            ),
        )

    state = _auth_state()

    session_factory = getattr(
        state,
        "session_factory",
        None,
    )

    if session_factory is None:
        return (
            None,
            None,
            _workspace_unavailable(),
        )

    try:
        with session_factory() as session:
            accounts = tenant_data.list_tenant_accounts(
                session,
                user_id=user.id,
                legacy_instagram_user_id=getattr(
                    state,
                    "legacy_instagram_user_id",
                    "",
                ),
            )

    except SQLAlchemyError:
        return (
            None,
            None,
            _workspace_unavailable(),
        )

    if not accounts:
        return (
            None,
            None,
            _cache_control(
                redirect(
                    url_for("onboarding")
                )
            ),
        )

    return user, accounts, None


def _selected_account(user, accounts):
    supplied_reference = request.args.get(
        ACCOUNT_REFERENCE_ARGUMENT
    )

    if not supplied_reference:
        return accounts[0]

    state = _auth_state()

    session_factory = getattr(
        state,
        "session_factory",
        None,
    )

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

    return next(
        (
            item
            for item in accounts
            if item.instagram_account_id == account.id
        ),
        None,
    )


def _account_payload(
    user,
    accounts,
    selected,
    *,
    include_disconnect_actions=False,
):
    payload = []

    for account in accounts:
        item = {
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
            "activation_detail": account.activation_detail,
            "is_selected": (
                account.instagram_account_id
                == selected.instagram_account_id
            ),
            "can_reconnect": account.can_reconnect,
            "can_disconnect": account.can_disconnect,
        }
        if include_disconnect_actions and account.can_disconnect:
            item["disconnect_action"] = action_reference_for_account(
                user.id,
                account.instagram_account_id,
                "disconnect",
                secret_key=current_app.secret_key,
            )
        payload.append(item)

    return payload


def _safe_account_unavailable():
    return (
        _cache_control(
            jsonify(
                error=ACCOUNT_UNAVAILABLE_MESSAGE
            )
        ),
        404,
    )


def _workspace_unavailable():
    return _cache_control(
        make_response(
            "Workspace temporarily unavailable.",
            503,
        )
    )


def _safe_lead_unavailable():
    return (
        _cache_control(
            jsonify(
                error=LEAD_UNAVAILABLE_MESSAGE
            )
        ),
        404,
    )


def _safe_lead_update_error():
    return (
        _cache_control(
            jsonify(
                error=LEAD_UPDATE_ERROR_MESSAGE
            )
        ),
        400,
    )


def _safe_automation_update_error():
    return (
        _cache_control(
            jsonify(
                error=AUTOMATION_UPDATE_ERROR_MESSAGE
            )
        ),
        400,
    )


def _safe_content_update_error():
    return (
        _cache_control(
            jsonify(
                error=CONTENT_UPDATE_ERROR_MESSAGE
            )
        ),
        400,
    )


def _content_draft_reference(user_id, item_id, *, purpose, now=None):
    return action_reference_for_account(
        user_id,
        item_id,
        purpose,
        secret_key=current_app.secret_key,
        now=now,
    )


def _resolve_owned_content_draft(session, *, user_id, selected_account_id, reference, purpose):
    if not isinstance(reference, str):
        return None

    parts = reference.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
        return None

    try:
        selected_account_uuid = uuid.UUID(str(selected_account_id))
    except (TypeError, ValueError, AttributeError):
        return None

    expires_at = int(parts[1])
    current = int(datetime.now(timezone.utc).timestamp())
    if expires_at < current or expires_at > current + 900:
        return None

    for draft in session.scalars(
        select(InstagramContentItem).where(
            InstagramContentItem.instagram_account_id == selected_account_uuid,
        )
    ):
        expected = _content_draft_reference(
            user_id,
            draft.id,
            purpose=purpose,
            now=expires_at - 900,
        )
        if hmac.compare_digest(expected, reference):
            return draft
    return None


def _safe_scheduler_action_error():
    return (
        _cache_control(
            jsonify(error="Scheduled job is unavailable.")
        ),
        400,
    )


def _render_page(
    template_name,
    *,
    user,
    accounts,
    selected,
    include_disconnect_actions=False,
    **context,
):
    account_payload = _account_payload(
        user,
        accounts,
        selected,
        include_disconnect_actions=include_disconnect_actions,
    )

    active_pages = {
        "tenant_dashboard.app_overview": "overview",
        "tenant_dashboard.app_accounts": "instagram",
        "tenant_dashboard.app_leads": "engagement",
        "tenant_dashboard.app_activity": "engagement",
        "tenant_dashboard.app_followups": "engagement",
        "tenant_dashboard.app_automations": "automations",
        "tenant_dashboard.app_messages": "engagement",
        "tenant_dashboard.app_content": "content",
        "tenant_dashboard.app_scheduler": "calendar",
        "tenant_dashboard.app_analytics": "overview",
    }

    selected_account = next(
        item
        for item in account_payload
        if item["is_selected"]
    )

    return _cache_control(
        render_template(
            template_name,
            current_user=user,
            accounts=account_payload,
            selected_account=selected_account,
            active_page=active_pages.get(
                request.endpoint,
                "",
            ),
            account_switch_endpoint=request.endpoint,
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

        selected = _selected_account(
            user,
            accounts,
        )

        if selected is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        if selected is None:
            return _safe_account_unavailable()

        return view(
            user,
            accounts,
            selected,
            *args,
            **kwargs,
        )

    return wrapped


def _query(
    selected,
    query,
    **kwargs,
):
    session_factory = getattr(
        _auth_state(),
        "session_factory",
        None,
    )

    if session_factory is None:
        return _DATABASE_UNAVAILABLE

    try:
        with session_factory() as session:
            return query(
                session,
                instagram_account_id=selected.instagram_account_id,
                **kwargs,
            )

    except SQLAlchemyError:
        return _DATABASE_UNAVAILABLE


def _scheduler_jobs_with_actions(user, selected, scheduler_page):
    """Attach only opaque, selected-account cancel actions to Scheduler rows."""

    rows = []
    for job in scheduler_page.jobs:
        row = dict(job)
        job_id = row.pop("_job_id", None)
        can_cancel = row.get("status_key") == "pending" and job_id is not None
        row["can_cancel"] = can_cancel
        row["cancel_action"] = (
            tenant_data.scheduler_cancel_reference_for_job(
                user.id,
                selected.instagram_account_id,
                job_id,
                secret_key=current_app.secret_key,
            )
            if can_cancel
            else None
        )
        rows.append(row)
    return rows


def _cancel_scheduler_job(user, selected):
    session_factory = getattr(_auth_state(), "session_factory", None)
    if session_factory is None:
        return _workspace_unavailable()
    try:
        with session_factory.begin() as session:
            cancelled = tenant_data.cancel_scheduler_followup(
                session,
                instagram_account_id=selected.instagram_account_id,
                reference=request.form.get("scheduler_action"),
                secret_key=current_app.secret_key,
                user_id=user.id,
            )
    except SQLAlchemyError:
        return _workspace_unavailable()
    if not cancelled:
        return _safe_account_unavailable()
    return _cache_control(
        redirect(
            url_for(
                "tenant_dashboard.app_scheduler",
                account=action_reference_for_account(
                    user.id,
                    selected.instagram_account_id,
                    ACCOUNT_REFERENCE_PURPOSE,
                    secret_key=current_app.secret_key,
                ),
                status=tenant_data.scheduler_status_filter(request.args.get("status")),
                job_type=tenant_data.scheduler_job_type_filter(request.args.get("job_type")),
            ),
            code=303,
        )
    )


def _lead_filters():
    return tenant_data.lead_filters(
        request.args.get("view"),
        request.args.get("stage"),
        request.args.get("q"),
        request.args.get("sort"),
        request.args.get("page"),
    )


def _activity_filter():
    return tenant_data.activity_filter(request.args.get("event_type"))


def _lead_payload(
    user,
    selected,
    rows,
):
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
                "prompt_url": (
                    f"/p/o/{row.public_prompt_id}"
                    if row.public_prompt_id
                    else None
                ),
                "post_permalink": row.post_permalink,
                "cover_url": row.cover_url,
                "status_action":
                    tenant_data.lead_action_reference_for_prompt_request(
                        user.id,
                        selected.instagram_account_id,
                        row.prompt_request_id,
                        "lead-status",
                        secret_key=current_app.secret_key,
                    ),
                "note_action":
                    tenant_data.lead_action_reference_for_prompt_request(
                        user.id,
                        selected.instagram_account_id,
                        row.prompt_request_id,
                        "lead-note",
                        secret_key=current_app.secret_key,
                    ),
                "archive_action":
                    tenant_data.lead_action_reference_for_prompt_request(
                        user.id,
                        selected.instagram_account_id,
                        row.prompt_request_id,
                        "lead-archive",
                        secret_key=current_app.secret_key,
                    ),
                "analysis_action":
                    tenant_data.lead_action_reference_for_prompt_request(
                        user.id,
                        selected.instagram_account_id,
                        row.prompt_request_id,
                        "lead-analysis",
                        secret_key=current_app.secret_key,
                    ),
                "ai_intent": row.ai_intent,
                "ai_priority": row.ai_priority,
                "ai_summary": row.ai_summary,
                "ai_suggested_followup": row.ai_suggested_followup,
                "ai_analyzed_at": row.ai_analyzed_at,
            }
        )

    return payload


def _lead_redirect():
    view, stage, search, sort, page = _lead_filters()

    return redirect(
        url_for(
            "tenant_dashboard.app_leads",
            account=request.args.get(
                ACCOUNT_REFERENCE_ARGUMENT
            ),
            view=view,
            stage=stage,
            q=search or None,
            sort=sort,
            page=page,
        ),
        code=303,
    )


def _mutate_lead(
    user,
    selected,
    purpose,
    mutate,
):
    state = _auth_state()

    session_factory = getattr(
        state,
        "session_factory",
        None,
    )

    if session_factory is None:
        return _safe_lead_unavailable()

    try:
        with session_factory.begin() as session:
            lead = (
                tenant_data.resolve_tenant_lead_action_reference(
                    session,
                    user_id=user.id,
                    instagram_account_id=selected.instagram_account_id,
                    reference=request.form.get(
                        "lead_action"
                    ),
                    purpose=purpose,
                    secret_key=current_app.secret_key,
                )
            )

            if lead is None:
                return _safe_lead_unavailable()

            mutate(lead)

    except SQLAlchemyError:
        return _safe_lead_unavailable()

    return _lead_redirect()


def _analyze_lead(user, selected):
    """Run an owner-requested advisory analysis outside database transactions."""

    session_factory = getattr(_auth_state(), "session_factory", None)
    if session_factory is None:
        return _safe_lead_unavailable()
    try:
        with session_factory.begin() as session:
            lead = tenant_data.resolve_tenant_lead_action_reference(
                session,
                user_id=user.id,
                instagram_account_id=selected.instagram_account_id,
                reference=request.form.get("lead_action"),
                purpose="lead-analysis",
                secret_key=current_app.secret_key,
            )
            if lead is None:
                return _safe_lead_unavailable()
            context = lead_analysis_context(session, lead=lead)
    except SQLAlchemyError:
        return _safe_lead_unavailable()

    analyzer = current_app.extensions.get("lead_intelligence_analyzer")
    if not callable(analyzer):
        return _lead_redirect()
    try:
        analysis = analyzer(context)
    except Exception:
        return _lead_redirect()
    if not isinstance(analysis, LeadIntelligence):
        return _lead_redirect()

    try:
        with session_factory.begin() as session:
            lead = tenant_data.resolve_tenant_lead_action_reference(
                session,
                user_id=user.id,
                instagram_account_id=selected.instagram_account_id,
                reference=request.form.get("lead_action"),
                purpose="lead-analysis",
                secret_key=current_app.secret_key,
            )
            if lead is None:
                return _safe_lead_unavailable()
            lead.ai_intent = analysis.intent
            lead.ai_priority = analysis.priority
            lead.ai_summary = analysis.summary
            lead.ai_suggested_followup = analysis.suggested_followup
            lead.ai_analyzed_at = datetime.now(timezone.utc)
    except SQLAlchemyError:
        return _safe_lead_unavailable()
    return _lead_redirect()


def _automation_config(selected):
    session_factory = getattr(
        _auth_state(),
        "session_factory",
        None,
    )

    if session_factory is None:
        return _DATABASE_UNAVAILABLE

    try:
        with session_factory() as session:
            config = session.scalar(
                select(
                    InstagramAutomationConfig
                ).where(
                    InstagramAutomationConfig.instagram_account_id
                    == selected.instagram_account_id
                )
            )

            if config is None:
                return None

            return {
                "enabled": config.enabled,
                "trigger_keywords": list(
                    config.trigger_keywords
                    or []
                ),
                "public_reply_enabled":
                    config.public_reply_enabled,
                "public_reply_text":
                    config.public_reply_text
                    or "",
                "followup_enabled": (
                    config.followup_enabled
                    if config.followup_enabled is not None
                    else True
                ),
                "followup_delay_minutes": (
                    config.followup_delay_minutes
                    if config.followup_delay_minutes is not None
                    else DEFAULT_FOLLOWUP_DELAY_MINUTES
                ),
                "smart_reply_enabled": config.smart_reply_enabled,
                "smart_reply_tone": config.smart_reply_tone,
                "smart_reply_max_per_hour": config.smart_reply_max_per_hour,
            }

    except SQLAlchemyError:
        return _DATABASE_UNAVAILABLE


def _update_automation_config(selected):
    session_factory = getattr(
        _auth_state(),
        "session_factory",
        None,
    )

    if session_factory is None:
        return _DATABASE_UNAVAILABLE

    try:
        raw_keywords = str(
            request.form.get(
                "trigger_keywords",
                "",
            )
            or ""
        )

        keyword_items = [
            item.strip()
            for item in raw_keywords.split(",")
            if item.strip()
        ]

        keywords = normalize_trigger_keywords(
            keyword_items
        )
        followup_delay_value = request.form.get("followup_delay_minutes")
        if followup_delay_value is None or str(followup_delay_value).strip() == "":
            followup_delay_minutes = DEFAULT_FOLLOWUP_DELAY_MINUTES
        else:
            followup_delay_minutes = normalize_followup_delay_minutes(
                followup_delay_value
            )
        smart_reply_tone_value = request.form.get("smart_reply_tone")
        smart_reply_max_per_hour_value = request.form.get(
            "smart_reply_max_per_hour"
        )
        smart_reply_controls_present = (
            smart_reply_tone_value is not None
            or smart_reply_max_per_hour_value is not None
            or "smart_reply_enabled" in request.form
        )
        if smart_reply_controls_present:
            smart_reply_tone = normalize_smart_reply_tone(
                smart_reply_tone_value
            )
            smart_reply_max_per_hour = normalize_smart_reply_max_per_hour(
                smart_reply_max_per_hour_value
            )

    except ValueError:
        return False

    try:
        with session_factory.begin() as session:
            config = session.scalar(
                select(
                    InstagramAutomationConfig
                ).where(
                    InstagramAutomationConfig.instagram_account_id
                    == selected.instagram_account_id
                )
            )

            if config is None:
                return None

            config.enabled = (
                request.form.get("enabled")
                == "on"
            )

            config.trigger_keywords = keywords

            config.public_reply_enabled = (
                request.form.get(
                    "public_reply_enabled"
                )
                == "on"
            )

            config.public_reply_text = str(
                request.form.get(
                    "public_reply_text"
                )
                or ""
            ).strip()
            config.followup_enabled = (
                request.form.get("followup_enabled") == "on"
                if "followup_enabled" in request.form
                else (config.followup_enabled if config.followup_enabled is not None else DEFAULT_FOLLOWUP_ENABLED)
            )
            config.followup_delay_minutes = followup_delay_minutes

            if smart_reply_controls_present:
                config.smart_reply_enabled = (
                    request.form.get("smart_reply_enabled") == "on"
                )
                config.smart_reply_tone = smart_reply_tone
                config.smart_reply_max_per_hour = smart_reply_max_per_hour

            session.flush()

    except SQLAlchemyError:
        return _DATABASE_UNAVAILABLE

    return True


def register_tenant_dashboard(app) -> None:
    dashboard = Blueprint(
        "tenant_dashboard",
        __name__,
        url_prefix="/app",
        template_folder="dashboard/templates",
    )

    @dashboard.after_request
    def tenant_no_store(response):
        return _cache_control(response)

    @dashboard.get("")
    @_require_tenant
    def app_overview(
        user,
        accounts,
        selected,
    ):
        data = _query(
            selected,
            tenant_data.overview,
        )

        if data is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return _render_page(
            "app_overview.html",
            user=user,
            accounts=accounts,
            selected=selected,
            overview=data,
        )

    @dashboard.get("/accounts")
    @_require_tenant
    def app_accounts(
        user,
        accounts,
        selected,
    ):
        return _render_page(
            "app_accounts.html",
            user=user,
            accounts=accounts,
            selected=selected,
            include_disconnect_actions=True,
            deployment_readiness=oauth_deployment_readiness(),
        )

    @dashboard.get("/leads")
    @_require_tenant
    def app_leads(
        user,
        accounts,
        selected,
    ):
        (
            view,
            stage,
            search,
            sort,
            page,
        ) = _lead_filters()

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
            leads=_lead_payload(
                user,
                selected,
                lead_page.leads,
            ),
            lead_page=lead_page,
            selected_view=view,
            selected_stage=stage,
            selected_search=search,
            selected_sort=sort,
            lead_statuses=tenant_data.LEAD_STATUSES,
        )

    @dashboard.get("/activity")
    @_require_tenant
    def app_activity(
        user,
        accounts,
        selected,
    ):
        selected_event_type = _activity_filter()
        rows = _query(
            selected,
            tenant_data.activity,
            event_type=selected_event_type,
        )

        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return _render_page(
            "app_activity.html",
            user=user,
            accounts=accounts,
            selected=selected,
            activity=rows,
            activity_filters=tenant_data.ACTIVITY_EVENT_TYPE_LABELS,
            selected_event_type=selected_event_type,
        )

    @dashboard.get("/followups")
    @_require_tenant
    def app_followups(
        user,
        accounts,
        selected,
    ):
        rows = _query(
            selected,
            tenant_data.followups,
        )

        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return _render_page(
            "app_followups.html",
            user=user,
            accounts=accounts,
            selected=selected,
            followups=rows,
        )

    @dashboard.get("/messages")
    @_require_tenant
    def app_messages(
        user,
        accounts,
        selected,
    ):
        selected_filter = tenant_data.message_filter(request.args.get("filter"))
        page = request.args.get("page")
        message_page = _query(
            selected,
            tenant_data.messages,
            filter_value=selected_filter,
            page=page,
        )

        if message_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return _render_page(
            "app_messages.html",
            user=user,
            accounts=accounts,
            selected=selected,
            messages=message_page.messages,
            message_page=message_page,
            message_filters=tenant_data.MESSAGE_FILTER_LABELS,
            selected_filter=selected_filter,
        )

    @dashboard.get("/content")
    @_require_tenant
    def app_content(
        user,
        accounts,
        selected,
    ):
        selected_filter = tenant_data.content_filter(request.args.get("filter"))
        page = request.args.get("page")
        content_page = _query(
            selected,
            tenant_data.content,
            filter_value=selected_filter,
            page=page,
        )

        if content_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        session_factory = getattr(_auth_state(), "session_factory", None)
        drafts = []
        if session_factory is not None:
            try:
                with session_factory() as session:
                    raw_drafts = tenant_data.content_drafts(
                        session,
                        instagram_account_id=selected.instagram_account_id,
                    )
                    drafts = [
                        {
                            **draft,
                            "draft_ref": _content_draft_reference(
                                user.id,
                                draft["id"],
                                purpose="content-draft-edit",
                            ) if draft.get("id") else None,
                            "status_ref": _content_draft_reference(
                                user.id,
                                draft["id"],
                                purpose="content-draft-status",
                            ) if draft.get("id") else None,
                            "delete_ref": _content_draft_reference(
                                user.id,
                                draft["id"],
                                purpose="content-draft-delete",
                            ) if draft.get("id") else None,
                            "schedule_ref": _content_draft_reference(user.id, draft["id"], purpose="content-schedule") if draft.get("id") else None,
                            "unschedule_ref": _content_draft_reference(user.id, draft["id"], purpose="content-unschedule") if draft.get("id") else None,
                        }
                        for draft in raw_drafts
                    ]
            except SQLAlchemyError:
                return _workspace_unavailable()

        return _render_page(
            "app_content.html",
            user=user,
            accounts=accounts,
            selected=selected,
            content_items=content_page.content,
            content_page=content_page,
            content_filters=tenant_data.CONTENT_FILTER_LABELS,
            selected_filter=selected_filter,
            drafts=drafts,
            content_types=CONTENT_TYPES,
        )

    @dashboard.post("/content/drafts/create")
    @_require_tenant
    def app_content_create_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()

        session_factory = getattr(_auth_state(), "session_factory", None)
        if session_factory is None:
            return _workspace_unavailable()

        try:
            with session_factory.begin() as session:
                media_value = (request.form.get("media_url") or request.form.get("media_reference") or "").strip()
                create_content_draft(
                    session,
                    instagram_account_id=selected.instagram_account_id,
                    content_type=request.form.get("content_type") or "image",
                    caption=request.form.get("caption") or "",
                    media_url=media_value,
                )
        except (SQLAlchemyError, ValueError):
            return _safe_content_update_error()

        return _cache_control(
            redirect(
                url_for(
                    "tenant_dashboard.app_content",
                    account=action_reference_for_account(
                        user.id,
                        selected.instagram_account_id,
                        ACCOUNT_REFERENCE_PURPOSE,
                        secret_key=current_app.secret_key,
                    ),
                ),
                code=303,
            )
        )

    @dashboard.post("/content/drafts/edit")
    @_require_tenant
    def app_content_edit_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()

        session_factory = getattr(_auth_state(), "session_factory", None)
        if session_factory is None:
            return _workspace_unavailable()

        draft_ref = str(request.form.get("draft_ref") or "").strip()
        if not draft_ref:
            return _safe_content_update_error()

        caption_value = request.form.get("caption")
        media_url_value = request.form.get("media_url")

        try:
            with session_factory.begin() as session:
                draft = _resolve_owned_content_draft(
                    session,
                    user_id=user.id,
                    selected_account_id=selected.instagram_account_id,
                    reference=draft_ref,
                    purpose="content-draft-edit",
                )
                if draft is None:
                    raise PermissionError("Draft not found for this account.")
                if draft.status == "scheduled":
                    raise ValueError("Scheduled content must be returned to ready before editing.")
                update_content_draft(
                    session,
                    selected.instagram_account_id,
                    draft.id,
                    content_type=request.form.get("content_type"),
                    caption=caption_value,
                    media_url=media_url_value,
                )
        except (PermissionError, SQLAlchemyError, ValueError):
            app.logger.exception(
                "Draft edit failed while processing a safe tenant content-draft-edit request."
            )
            return _safe_content_update_error()

        return _cache_control(
            redirect(
                url_for(
                    "tenant_dashboard.app_content",
                    account=action_reference_for_account(
                        user.id,
                        selected.instagram_account_id,
                        ACCOUNT_REFERENCE_PURPOSE,
                        secret_key=current_app.secret_key,
                    ),
                ),
                code=303,
            )
        )

    @dashboard.post("/content/drafts/status")
    @_require_tenant
    def app_content_status_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()

        status_value = str(request.form.get("status") or "").strip().lower()
        if status_value not in {"draft", "ready"}:
            return _safe_content_update_error()

        session_factory = getattr(_auth_state(), "session_factory", None)
        if session_factory is None:
            return _workspace_unavailable()

        draft_ref = str(request.form.get("draft_ref") or "").strip()
        if not draft_ref:
            return _safe_content_update_error()

        try:
            with session_factory.begin() as session:
                draft = _resolve_owned_content_draft(
                    session,
                    user_id=user.id,
                    selected_account_id=selected.instagram_account_id,
                    reference=draft_ref,
                    purpose="content-draft-status",
                )
                if draft is None:
                    raise PermissionError("Draft not found for this account.")
                transition_content_status(
                    session,
                    selected.instagram_account_id,
                    draft.id,
                    status_value,
                )
        except (PermissionError, SQLAlchemyError, ValueError):
            return _safe_content_update_error()

        return _cache_control(
            redirect(
                url_for(
                    "tenant_dashboard.app_content",
                    account=action_reference_for_account(
                        user.id,
                        selected.instagram_account_id,
                        ACCOUNT_REFERENCE_PURPOSE,
                        secret_key=current_app.secret_key,
                    ),
                ),
                code=303,
            )
        )

    @dashboard.post("/content/drafts/delete")
    @_require_tenant
    def app_content_delete_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()

        session_factory = getattr(_auth_state(), "session_factory", None)
        if session_factory is None:
            return _workspace_unavailable()

        draft_ref = str(request.form.get("draft_ref") or "").strip()
        if not draft_ref:
            return _safe_content_update_error()

        try:
            with session_factory.begin() as session:
                draft = _resolve_owned_content_draft(
                    session,
                    user_id=user.id,
                    selected_account_id=selected.instagram_account_id,
                    reference=draft_ref,
                    purpose="content-draft-delete",
                )
                if draft is None:
                    raise PermissionError("Draft not found for this account.")
                delete_content_draft(
                    session,
                    selected.instagram_account_id,
                    draft.id,
                )
        except (PermissionError, SQLAlchemyError, ValueError):
            return _safe_content_update_error()

        return _cache_control(
            redirect(
                url_for(
                    "tenant_dashboard.app_content",
                    account=action_reference_for_account(
                        user.id,
                        selected.instagram_account_id,
                        ACCOUNT_REFERENCE_PURPOSE,
                        secret_key=current_app.secret_key,
                    ),
                ),
                code=303,
            )
        )

    @dashboard.post("/content/drafts/schedule")
    @_require_tenant
    def app_content_schedule_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()
        draft_ref = str(request.form.get("draft_ref") or "").strip()
        try:
            local_time = datetime.fromisoformat(f"{request.form.get('date', '')}T{request.form.get('time', '')}")
            scheduled_for = local_time.replace(tzinfo=ZoneInfo("Africa/Casablanca"))
            session_factory = getattr(_auth_state(), "session_factory", None)
            if session_factory is None:
                return _workspace_unavailable()
            with session_factory.begin() as session:
                draft = _resolve_owned_content_draft(session, user_id=user.id, selected_account_id=selected.instagram_account_id, reference=draft_ref, purpose="content-schedule")
                if draft is None:
                    raise PermissionError("Draft not found for this account.")
                schedule_content_draft(session, selected.instagram_account_id, draft.id, scheduled_for)
        except (PermissionError, SQLAlchemyError, ValueError):
            return _safe_content_update_error()
        return _cache_control(redirect(url_for("tenant_dashboard.app_content", account=action_reference_for_account(user.id, selected.instagram_account_id, ACCOUNT_REFERENCE_PURPOSE, secret_key=current_app.secret_key)), code=303))

    @dashboard.post("/content/drafts/unschedule")
    @_require_tenant
    def app_content_unschedule_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()
        draft_ref = str(request.form.get("draft_ref") or "").strip()
        try:
            session_factory = getattr(_auth_state(), "session_factory", None)
            if session_factory is None:
                return _workspace_unavailable()
            with session_factory.begin() as session:
                draft = _resolve_owned_content_draft(session, user_id=user.id, selected_account_id=selected.instagram_account_id, reference=draft_ref, purpose="content-unschedule")
                if draft is None:
                    raise PermissionError("Draft not found for this account.")
                unschedule_content_draft(session, selected.instagram_account_id, draft.id)
        except (PermissionError, SQLAlchemyError, ValueError):
            return _safe_content_update_error()
        return _cache_control(redirect(url_for("tenant_dashboard.app_content", account=action_reference_for_account(user.id, selected.instagram_account_id, ACCOUNT_REFERENCE_PURPOSE, secret_key=current_app.secret_key)), code=303))

    @dashboard.get("/scheduler")
    @_require_tenant
    def app_scheduler(
        user,
        accounts,
        selected,
    ):
        selected_status = tenant_data.scheduler_status_filter(
            request.args.get("status")
        )
        selected_job_type = tenant_data.scheduler_job_type_filter(
            request.args.get("job_type")
        )
        selected_content_status = str(
            request.args.get("content_status") or "all"
        ).strip().lower()
        if selected_content_status not in {"all", "scheduled", "published", "failed"}:
            selected_content_status = "all"
        page = request.args.get("page")
        scheduler_page = _query(
            selected,
            tenant_data.scheduler_jobs,
            status_filter=selected_status,
            job_type_filter=selected_job_type,
            page=page,
        )

        if scheduler_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        agenda_content = []
        session_factory = getattr(_auth_state(), "session_factory", None)
        if session_factory is not None:
            try:
                with session_factory() as session:
                    agenda_content = [
                        {
                            **draft,
                            "unschedule_ref": _content_draft_reference(
                                user.id,
                                draft["id"],
                                purpose="content-unschedule",
                            ) if draft.get("status") == "scheduled" else None,
                            "display_time": (
                                draft.get("scheduled_for")
                                if draft.get("status") == "scheduled"
                                else draft.get("published_at") or draft.get("updated_at")
                            ),
                        }
                        for draft in tenant_data.content_drafts(session, instagram_account_id=selected.instagram_account_id)
                        if draft.get("status") in {"scheduled", "published", "failed"}
                        and (
                            selected_content_status == "all"
                            or draft.get("status") == selected_content_status
                        )
                    ]
                    for item in agenda_content:
                        item["agenda_day"] = (
                            item["display_time"][:10]
                            if item.get("display_time")
                            else "Date unavailable"
                        )
                    agenda_content.sort(
                        key=lambda item: (
                            item["agenda_day"] == "Date unavailable",
                            item.get("display_time") or "",
                        )
                    )
            except SQLAlchemyError:
                return _workspace_unavailable()

        return _render_page(
            "app_scheduler.html",
            user=user,
            accounts=accounts,
            selected=selected,
            jobs=_scheduler_jobs_with_actions(user, selected, scheduler_page),
            scheduler_page=scheduler_page,
            status_filters=tenant_data.SCHEDULER_STATUS_FILTER_LABELS,
            job_type_filters=tenant_data.SCHEDULER_JOB_TYPE_FILTER_LABELS,
            selected_status=selected_status,
            selected_job_type=selected_job_type,
            agenda_content=agenda_content,
            selected_content_status=selected_content_status,
        )

    @dashboard.get("/analytics")
    @_require_tenant
    def app_analytics(
        user,
        accounts,
        selected,
    ):
        selected_range = tenant_data.analytics_range_filter(
            request.args.get("range")
        )
        data = _query(
            selected,
            tenant_data.analytics,
            range_key=selected_range,
        )

        if data is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return _render_page(
            "app_analytics.html",
            user=user,
            accounts=accounts,
            selected=selected,
            analytics=data,
            range_filters=tenant_data.ANALYTICS_RANGE_LABELS,
            selected_range=selected_range,
        )

    @dashboard.post("/scheduler/cancel")
    @_require_tenant
    def app_scheduler_cancel(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_scheduler_action_error()
        return _cancel_scheduler_job(user, selected)

    @dashboard.route(
        "/automations",
        methods=[
            "GET",
            "POST",
        ],
    )
    @_require_tenant
    def app_automations(
        user,
        accounts,
        selected,
    ):
        if request.method == "POST":
            if not validate_csrf_token(
                request_csrf_token()
            ):
                return _safe_automation_update_error()

            updated = _update_automation_config(
                selected
            )

            if updated is _DATABASE_UNAVAILABLE:
                return _workspace_unavailable()

            if updated is None:
                return _safe_account_unavailable()

            if updated is False:
                return _safe_automation_update_error()

        automation = _automation_config(
            selected
        )

        if automation is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        if automation is None:
            return _safe_account_unavailable()

        return _render_page(
            "app_automations.html",
            user=user,
            accounts=accounts,
            selected=selected,
            automation=automation,
            smart_reply_tones=SMART_REPLY_TONES,
        )

    @dashboard.get("/api/accounts")
    @_require_tenant
    def app_api_accounts(
        user,
        accounts,
        selected,
    ):
        return jsonify(
            accounts=_account_payload(
                user,
                accounts,
                selected,
            )
        )

    @dashboard.get("/api/overview")
    @_require_tenant
    def app_api_overview(
        user,
        accounts,
        selected,
    ):
        data = _query(
            selected,
            tenant_data.overview,
        )

        if data is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        payload = _account_payload(
            user,
            accounts,
            selected,
        )

        if len(accounts) == 1:
            account = payload[0]
        else:
            account = next(
                item
                for item in payload
                if item["is_selected"]
            )

        return jsonify(
            account=account,
            overview=data,
        )

    @dashboard.get("/api/leads")
    @_require_tenant
    def app_api_leads(
        user,
        accounts,
        selected,
    ):
        (
            view,
            stage,
            search,
            sort,
            page,
        ) = _lead_filters()

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
            leads=_lead_payload(
                user,
                selected,
                lead_page.leads,
            ),
            filters={
                "view": view,
                "stage": stage,
                "q": search,
                "sort": sort,
            },
            pagination={
                "page": lead_page.page,
                "has_previous":
                    lead_page.has_previous,
                "has_next":
                    lead_page.has_next,
            },
        )

    @dashboard.get("/api/activity")
    @_require_tenant
    def app_api_activity(
        user,
        accounts,
        selected,
    ):
        rows = _query(
            selected,
            tenant_data.activity,
            event_type=_activity_filter(),
        )

        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return jsonify(
            activity=rows
        )

    @dashboard.get("/api/followups")
    @_require_tenant
    def app_api_followups(
        user,
        accounts,
        selected,
    ):
        rows = _query(
            selected,
            tenant_data.followups,
        )

        if rows is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return jsonify(
            followups=rows
        )

    @dashboard.get("/api/messages")
    @_require_tenant
    def app_api_messages(
        user,
        accounts,
        selected,
    ):
        selected_filter = tenant_data.message_filter(request.args.get("filter"))
        page = request.args.get("page")
        message_page = _query(
            selected,
            tenant_data.messages,
            filter_value=selected_filter,
            page=page,
        )

        if message_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return jsonify(
            messages=message_page.messages,
            pagination={
                "page": message_page.page,
                "has_previous": message_page.has_previous,
                "has_next": message_page.has_next,
            },
        )

    @dashboard.get("/api/content")
    @_require_tenant
    def app_api_content(
        user,
        accounts,
        selected,
    ):
        selected_filter = tenant_data.content_filter(request.args.get("filter"))
        page = request.args.get("page")
        content_page = _query(
            selected,
            tenant_data.content,
            filter_value=selected_filter,
            page=page,
        )

        if content_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return jsonify(
            content=content_page.content,
            pagination={
                "page": content_page.page,
                "has_previous": content_page.has_previous,
                "has_next": content_page.has_next,
            },
        )

    @dashboard.get("/api/scheduler")
    @_require_tenant
    def app_api_scheduler(
        user,
        accounts,
        selected,
    ):
        selected_status = tenant_data.scheduler_status_filter(
            request.args.get("status")
        )
        selected_job_type = tenant_data.scheduler_job_type_filter(
            request.args.get("job_type")
        )
        page = request.args.get("page")
        scheduler_page = _query(
            selected,
            tenant_data.scheduler_jobs,
            status_filter=selected_status,
            job_type_filter=selected_job_type,
            page=page,
        )

        if scheduler_page is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return jsonify(
            jobs=_scheduler_jobs_with_actions(user, selected, scheduler_page),
            pagination={
                "page": scheduler_page.page,
                "has_previous": scheduler_page.has_previous,
                "has_next": scheduler_page.has_next,
            },
        )

    @dashboard.get("/api/analytics")
    @_require_tenant
    def app_api_analytics(
        user,
        accounts,
        selected,
    ):
        selected_range = tenant_data.analytics_range_filter(
            request.args.get("range")
        )
        data = _query(
            selected,
            tenant_data.analytics,
            range_key=selected_range,
        )

        if data is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return jsonify(
            range=data.range_key,
            metrics=data.metrics,
            followup_breakdown=data.followup_breakdown,
            lead_breakdown=data.lead_breakdown,
            engagement_breakdown=data.engagement_breakdown,
            recent_failures=data.recent_failures,
        )

    @dashboard.post("/leads/status")
    @_require_tenant
    def app_lead_status(
        user,
        accounts,
        selected,
    ):
        if not validate_csrf_token(
            request_csrf_token()
        ):
            return _safe_lead_update_error()

        status = str(
            request.form.get(
                "lead_status"
            )
            or ""
        ).strip().lower()

        if status not in tenant_data.LEAD_STATUSES:
            return _safe_lead_update_error()

        return _mutate_lead(
            user,
            selected,
            "lead-status",
            lambda lead: setattr(
                lead,
                "lead_status",
                status,
            ),
        )

    @dashboard.post("/leads/note")
    @_require_tenant
    def app_lead_note(
        user,
        accounts,
        selected,
    ):
        if not validate_csrf_token(
            request_csrf_token()
        ):
            return _safe_lead_update_error()

        note = str(
            request.form.get(
                "internal_note"
            )
            or ""
        ).strip()

        if len(note) > MAX_INTERNAL_NOTE_LENGTH:
            return _safe_lead_update_error()

        return _mutate_lead(
            user,
            selected,
            "lead-note",
            lambda lead: setattr(
                lead,
                "internal_note",
                note or None,
            ),
        )

    @dashboard.post("/leads/archive")
    @_require_tenant
    def app_lead_archive(
        user,
        accounts,
        selected,
    ):
        if not validate_csrf_token(
            request_csrf_token()
        ):
            return _safe_lead_update_error()

        operation = str(
            request.form.get(
                "operation"
            )
            or ""
        ).strip().lower()

        if operation not in {
            "archive",
            "unarchive",
        }:
            return _safe_lead_update_error()

        archived_at = (
            datetime.now(timezone.utc)
            if operation == "archive"
            else None
        )

        return _mutate_lead(
            user,
            selected,
            "lead-archive",
            lambda lead: setattr(
                lead,
                "lead_archived_at",
                archived_at,
            ),
        )

    @dashboard.post("/leads/analyze")
    @_require_tenant
    def app_lead_analyze(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_lead_update_error()
        return _analyze_lead(user, selected)

    app.register_blueprint(
        dashboard
    )
