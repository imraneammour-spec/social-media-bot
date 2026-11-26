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
    SUPPORT_EMAIL,
    action_reference_for_account,
    authentication_available,
    csrf_token,
    current_user_has_legacy_workspace_access,
    get_current_user,
    login_required,
    request_csrf_token,
    resolve_owned_account_action_reference,
    USER_SESSION_KEY,
    validate_csrf_token,
)
from database import tenant_dashboard as tenant_data
from database.admin_dashboard import admin_snapshot
from database.tenant_insights import direct_interactions
from dashboard.presentation import display_time, public_image_url
from dashboard.i18n import LOCALE_META, LOCALE_SESSION_KEY, locale_context, normalize_locale, translate, session_locale
from database.lead_intelligence import LeadIntelligence, lead_analysis_context
from database.oauth_deployment import oauth_deployment_readiness
from database.onboarding import user_onboarding_is_complete
from database.content import (
    CONTENT_TYPES,
    MAX_CAPTION_LENGTH,
    create_content_draft,
    delete_content_draft,
    recover_failed_content_draft,
    schedule_content_draft,
    transition_content_status,
    unschedule_content_draft,
    update_content_draft,
)
from database.content_media import (
    ContentMediaProviderError,
    ContentMediaUnavailableError,
    ContentMediaValidationError,
    content_media_is_configured,
    delete_content_image,
    upload_content_image,
)
from database.models import InstagramAutomationConfig, InstagramContentItem
from database.tenant_automation import (
    AUTOMATION_DELIVERY_MODES,
    SMART_REPLY_TONES,
    TenantAutomationConfigurationError,
    automation_mode_defaults,
    automation_mode_values,
    normalize_automation_delivery_mode,
    normalize_destination_url,
    normalize_followup_delay_minutes,
    normalize_inactive_mode_settings,
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
AUTOMATION_DESTINATION_URL_ERROR_MESSAGE = (
    "Enter a valid http:// or https:// destination URL."
)
LEGACY_AUTOMATION_READONLY_MESSAGE = (
    "Automation settings cannot be changed for the server-managed account."
)
LEGACY_SCHEDULING_UNAVAILABLE_MESSAGE = (
    "Scheduling is not available for the server-managed account. "
    "Save the draft here, then publish it manually in Instagram."
)
CONTENT_UPDATE_ERROR_MESSAGE = "Draft update could not be completed."
CONTENT_IMAGE_TOO_LARGE_MESSAGE = "Choose a JPEG or JPG image that is 8 MB or smaller."
CONTENT_IMAGE_INVALID_MESSAGE = "Choose a valid JPEG or JPG image."
CONTENT_UPLOAD_UNAVAILABLE_MESSAGE = "Image upload is temporarily unavailable. You can use a public HTTPS image link instead."
CONTENT_UPLOAD_FAILED_MESSAGE = "Image upload could not be completed. Try again or use a public HTTPS image link."
CONTENT_SCHEDULE_INVALID_MESSAGE = "Choose a future date and time."

MAX_INTERNAL_NOTE_LENGTH = 2000

_DATABASE_UNAVAILABLE = object()
_INVALID_DESTINATION_URL = object()


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
            if not user_onboarding_is_complete(session, user.id):
                return (
                    None,
                    None,
                    _cache_control(redirect(url_for("onboarding"))),
                )
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


def _ui_text(message):
    return translate(session_locale(browser_session), message)


def _safe_account_unavailable():
    return (
        _cache_control(
            jsonify(
                error=_ui_text(ACCOUNT_UNAVAILABLE_MESSAGE)
            )
        ),
        404,
    )


def _workspace_unavailable():
    return _cache_control(
        make_response(
            _ui_text("Workspace temporarily unavailable."),
            503,
        )
    )


def _safe_lead_unavailable():
    return (
        _cache_control(
            jsonify(
                error=_ui_text(LEAD_UNAVAILABLE_MESSAGE)
            )
        ),
        404,
    )


def _safe_lead_update_error():
    return (
        _cache_control(
            jsonify(
                error=_ui_text(LEAD_UPDATE_ERROR_MESSAGE)
            )
        ),
        400,
    )


def _safe_automation_update_error():
    return (
        _cache_control(
            jsonify(
                error=_ui_text(AUTOMATION_UPDATE_ERROR_MESSAGE)
            )
        ),
        400,
    )


def _safe_automation_destination_url_error():
    return (
        _cache_control(
            jsonify(
                error=_ui_text(AUTOMATION_DESTINATION_URL_ERROR_MESSAGE)
            )
        ),
        400,
    )


def _safe_content_update_error():
    return (
        _cache_control(
            jsonify(
                error=_ui_text(CONTENT_UPDATE_ERROR_MESSAGE)
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


def _content_storage_asset_id(item) -> str | None:
    metadata = getattr(item, "metadata_json", None)
    storage = metadata.get("storage") if isinstance(metadata, dict) else None
    if not isinstance(storage, dict) or storage.get("provider") != "cloudinary":
        return None
    asset_id = str(storage.get("asset_id") or "").strip()
    return asset_id or None


def _safe_scheduler_action_error():
    return (
        _cache_control(
            jsonify(error=_ui_text("Scheduled job is unavailable."))
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
        "tenant_dashboard.app_analytics": "analytics",
        "tenant_dashboard.app_support": "support",
    }

    selected_account = next(
        item
        for item in account_payload
        if item["is_selected"]
    )

    locale = locale_context(session_locale(browser_session))
    if "internal_prompt_access" not in context:
        context["internal_prompt_access"] = current_user_has_legacy_workspace_access()
    context["internal_admin_access"] = bool(context["internal_prompt_access"])
    if "is_legacy_account" not in context:
        context["is_legacy_account"] = bool(getattr(selected, "is_legacy", False))
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
            locale=locale,
            locale_options=LOCALE_META,
            t=lambda key, **values: translate(locale["code"], key, **values),
            display_time=lambda value: display_time(value, locale["code"]),
            display_username=lambda value: translate(locale["code"], "Instagram user") if value == "Instagram user" else value,
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
                "tenant_dashboard.app_followups",
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


_INTERNAL_PROMPT_CONTENT_FIELDS = frozenset({
    "prompt_url",
    "public_id",
    "prompt_count",
})


def _content_payload(rows, *, internal_prompt_access):
    if internal_prompt_access:
        return rows
    return [
        {
            key: value
            for key, value in row.items()
            if key not in _INTERNAL_PROMPT_CONTENT_FIELDS
        }
        for row in rows
    ]


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


def _legacy_automation_presentation():
    """Return the read-only presentation for the legacy account.

    The server-managed account has no tenant automation row and its
    engagement automation runs through the legacy bot, so the tenant
    settings page shows the legacy bot as active (see
    ``database.tenant_dashboard.legacy_automation_status``) instead of 404.
    The form itself is rendered disabled; this presentation only decides
    which toggles appear on.
    """

    modes = {}
    for mode in ("prompt_request", "direct_message", "link"):
        values = automation_mode_defaults(mode)
        values["defaults"] = automation_mode_defaults(mode)
        modes[mode] = values
    active_values = modes["prompt_request"]
    return {
        "enabled": tenant_data.resolve_automation_enabled(
            is_legacy=True, config_enabled=False
        ),
        "modes": modes,
        "trigger_keywords": active_values["trigger_keywords"],
        "public_reply_enabled": active_values["public_reply_enabled"],
        "public_reply_text": active_values["public_reply_text"],
        "delivery_mode": "prompt_request",
        "private_message_text": active_values["private_message_text"],
        "destination_url": active_values["destination_url"],
        "followup_enabled": active_values["followup_enabled"],
        "followup_delay_minutes": active_values["followup_delay_minutes"],
        "followup_message_text": active_values["followup_message_text"],
        "smart_reply_enabled": True,
        "smart_reply_tone": "friendly",
        "smart_reply_max_per_hour": 20,
    }


def _automation_config(selected, *, internal_prompt_access=False):
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
                if getattr(selected, "is_legacy", False):
                    return _legacy_automation_presentation()
                return None

            saved_delivery_mode = (
                getattr(config, "delivery_mode", "prompt_request")
                or "prompt_request"
            )
            delivery_mode = saved_delivery_mode
            if delivery_mode == "prompt_request" and not internal_prompt_access:
                delivery_mode = "direct_message"
            visible_modes = ["direct_message", "link"]
            if internal_prompt_access:
                visible_modes.insert(0, "prompt_request")
            modes = {
                mode: automation_mode_values(config, mode)
                for mode in visible_modes
            }
            for mode, values in modes.items():
                values["defaults"] = automation_mode_defaults(mode)
            active_values = modes[delivery_mode]
            return {
                "enabled": config.enabled,
                "modes": modes,
                "trigger_keywords": active_values["trigger_keywords"],
                "public_reply_enabled": active_values["public_reply_enabled"],
                "public_reply_text": active_values["public_reply_text"],
                "delivery_mode": delivery_mode,
                "private_message_text": active_values["private_message_text"],
                "destination_url": active_values["destination_url"],
                "followup_enabled": active_values["followup_enabled"],
                "followup_delay_minutes": active_values["followup_delay_minutes"],
                "followup_message_text": active_values["followup_message_text"],
                "smart_reply_enabled": config.smart_reply_enabled,
                "smart_reply_tone": config.smart_reply_tone,
                "smart_reply_max_per_hour": config.smart_reply_max_per_hour,
            }

    except SQLAlchemyError:
        return _DATABASE_UNAVAILABLE


_MODE_FORM_PREFIXES = {
    "prompt_request": "prompt",
    "direct_message": "message",
    "link": "link",
}


def _mode_form_value(mode, field, selected_mode):
    prefixed_name = f"{_MODE_FORM_PREFIXES[mode]}_{field}"
    if prefixed_name in request.form:
        return request.form.get(prefixed_name), True
    if mode != selected_mode:
        return None, False
    legacy_name = field
    if mode == "prompt_request" and field == "private_message_text":
        legacy_name = "prompt_opening_message_text"
    if legacy_name in request.form:
        return request.form.get(legacy_name), True
    return None, False


def _submitted_mode_values(mode, selected_mode, current_values):
    """Validate one mode panel while retaining every omitted saved value."""

    values = dict(current_values)
    raw_keywords, present = _mode_form_value(
        mode, "trigger_keywords", selected_mode
    )
    if present:
        values["trigger_keywords"] = normalize_trigger_keywords([
            item.strip()
            for item in str(raw_keywords or "").split(",")
            if item.strip()
        ])
    for field in ("public_reply_enabled", "followup_enabled"):
        raw_value, present = _mode_form_value(mode, field, selected_mode)
        if present:
            values[field] = raw_value == "on"
    for field in (
        "public_reply_text",
        "private_message_text",
        "followup_message_text",
    ):
        raw_value, present = _mode_form_value(mode, field, selected_mode)
        if present:
            text_value = str(raw_value or "").strip()
            if text_value:
                values[field] = text_value
    raw_delay, present = _mode_form_value(
        mode, "followup_delay_minutes", selected_mode
    )
    if present:
        values["followup_delay_minutes"] = normalize_followup_delay_minutes(raw_delay)
    raw_destination, present = _mode_form_value(
        mode, "destination_url", selected_mode
    )
    if mode == "link" and present:
        destination = str(raw_destination or "").strip()
        values["destination_url"] = (
            normalize_destination_url(destination) if destination else ""
        )
    return values


def _submitted_automation_presentation(automation):
    """Return unsaved browser values without applying persistence validation."""

    presentation = dict(automation)
    modes = {
        mode: dict(values)
        for mode, values in automation["modes"].items()
    }
    requested_mode = str(request.form.get("delivery_mode") or "").strip()
    selected_mode = (
        requested_mode
        if requested_mode in modes
        else automation["delivery_mode"]
    )

    for mode, values in modes.items():
        raw_keywords, present = _mode_form_value(
            mode, "trigger_keywords", selected_mode
        )
        if present:
            values["trigger_keywords"] = [
                item.strip()
                for item in str(raw_keywords or "").split(",")
                if item.strip()
            ]
        for field in ("public_reply_enabled", "followup_enabled"):
            raw_value, present = _mode_form_value(mode, field, selected_mode)
            if present:
                values[field] = raw_value == "on"
        for field in (
            "public_reply_text",
            "private_message_text",
            "followup_delay_minutes",
            "followup_message_text",
            "destination_url",
        ):
            raw_value, present = _mode_form_value(mode, field, selected_mode)
            if present:
                values[field] = str(raw_value or "").strip()

    presentation["modes"] = modes
    presentation["delivery_mode"] = selected_mode
    presentation["enabled"] = request.form.get("enabled") == "on"
    if "smart_reply_enabled" in request.form:
        presentation["smart_reply_enabled"] = (
            request.form.get("smart_reply_enabled") == "on"
        )
    for field in ("smart_reply_tone", "smart_reply_max_per_hour"):
        if field in request.form:
            presentation[field] = str(request.form.get(field) or "").strip()
    return presentation


def _update_automation_config(selected, *, internal_prompt_access=False):
    session_factory = getattr(
        _auth_state(),
        "session_factory",
        None,
    )

    if session_factory is None:
        return _DATABASE_UNAVAILABLE

    try:
        smart_reply_tone_value = request.form.get("smart_reply_tone")
        smart_reply_max_per_hour_value = request.form.get(
            "smart_reply_max_per_hour"
        )
        delivery_mode_value = request.form.get("delivery_mode")
        delivery_mode = (
            normalize_automation_delivery_mode(delivery_mode_value)
            if delivery_mode_value is not None
            else None
        )
        if (
            delivery_mode_value is not None
            and delivery_mode == "prompt_request"
            and not internal_prompt_access
        ):
            return False
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

            if delivery_mode is None:
                delivery_mode = normalize_automation_delivery_mode(
                    getattr(config, "delivery_mode", "prompt_request")
                    or "prompt_request"
                )

            allowed_modes = ["direct_message", "link"]
            if internal_prompt_access:
                allowed_modes.insert(0, "prompt_request")
            mode_values = {
                mode: automation_mode_values(config, mode)
                for mode in AUTOMATION_DELIVERY_MODES
            }
            for mode in allowed_modes:
                try:
                    mode_values[mode] = _submitted_mode_values(
                        mode, delivery_mode, mode_values[mode]
                    )
                except TenantAutomationConfigurationError:
                    if mode != delivery_mode:
                        continue
                    if delivery_mode == "link":
                        return _INVALID_DESTINATION_URL
                    return False
            selected_values = mode_values[delivery_mode]
            if delivery_mode == "link" and not selected_values["destination_url"]:
                return _INVALID_DESTINATION_URL

            config.enabled = (
                request.form.get("enabled")
                == "on"
            )

            config.trigger_keywords = list(selected_values["trigger_keywords"])
            config.public_reply_enabled = bool(selected_values["public_reply_enabled"])
            config.public_reply_text = str(selected_values["public_reply_text"])
            config.delivery_mode = delivery_mode
            if delivery_mode == "prompt_request":
                config.prompt_opening_message_text = str(
                    selected_values["private_message_text"]
                )
            else:
                config.private_message_text = str(
                    selected_values["private_message_text"]
                )
            if delivery_mode == "link":
                config.destination_url = str(selected_values["destination_url"])
            config.followup_enabled = bool(selected_values["followup_enabled"])
            config.followup_delay_minutes = int(
                selected_values["followup_delay_minutes"]
            )
            config.followup_message_text = str(selected_values["followup_message_text"])

            inactive_modes = {
                mode: values
                for mode, values in mode_values.items()
                if mode != delivery_mode
            }
            config.inactive_mode_settings = normalize_inactive_mode_settings({
                "version": 1,
                "modes": inactive_modes,
            })

            if smart_reply_controls_present:
                config.smart_reply_enabled = (
                    request.form.get("smart_reply_enabled") == "on"
                )
                config.smart_reply_tone = smart_reply_tone
                config.smart_reply_max_per_hour = smart_reply_max_per_hour

            session.flush()

    except ValueError:
        return False
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

    @dashboard.post("/locale")
    @_require_tenant
    def app_locale(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()
        browser_session[LOCALE_SESSION_KEY] = normalize_locale(request.form.get("locale"))
        return _cache_control(redirect(request.referrer or url_for("tenant_dashboard.app_overview"), code=303))

    @dashboard.after_request
    def tenant_no_store(response):
        return _cache_control(response)

    @dashboard.get("/admin")
    @login_required
    def app_admin():
        if not current_user_has_legacy_workspace_access():
            return make_response("Forbidden.", 403)

        user = get_current_user()
        state = _auth_state()
        session_factory = getattr(state, "session_factory", None)
        if user is None or session_factory is None:
            return _workspace_unavailable()

        try:
            with session_factory() as session:
                snapshot = admin_snapshot(session)
        except SQLAlchemyError:
            return _workspace_unavailable()

        return render_template(
            "app_admin.html",
            current_user=user,
            admin=snapshot,
            display_time=lambda value: display_time(value, "en"),
        )

    @dashboard.get("/onboarding")
    def app_onboarding():
        """Redirect the legacy /app/onboarding URL to the real onboarding flow."""

        return _cache_control(redirect(url_for("onboarding"), code=303))

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
            is_legacy=getattr(selected, "is_legacy", False),
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

    @dashboard.get("/support")
    @_require_tenant
    def app_support(
        user,
        accounts,
        selected,
    ):
        return _render_page(
            "app_support.html",
            user=user,
            accounts=accounts,
            selected=selected,
            support_email=SUPPORT_EMAIL,
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

        interactions = _query(selected, direct_interactions)
        if lead_page is _DATABASE_UNAVAILABLE or interactions is _DATABASE_UNAVAILABLE:
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
            direct_interactions=interactions,
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
        status = tenant_data.scheduler_status_filter(request.args.get("status"))
        page = _query(selected, tenant_data.scheduler_jobs, status_filter=status, page=request.args.get("page"))
        waiting = _query(selected, direct_interactions, waiting_only=True)

        if page is _DATABASE_UNAVAILABLE or waiting is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        return _render_page(
            "app_followups.html",
            user=user,
            accounts=accounts,
            selected=selected,
            followups=_scheduler_jobs_with_actions(user, selected, page),
            waiting=waiting,
            scheduler_page=page,
            selected_status=status,
            status_filters=tenant_data.SCHEDULER_STATUS_FILTER_LABELS,
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

    def _render_content_page(
        user,
        accounts,
        selected,
        *,
        content_error=None,
        content_form=None,
        content_edit_form=None,
        status=200,
    ):
        internal_prompt_access = current_user_has_legacy_workspace_access()
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
                    submitted_edit_draft = None
                    if content_edit_form:
                        submitted_edit_draft = _resolve_owned_content_draft(
                            session,
                            user_id=user.id,
                            selected_account_id=selected.instagram_account_id,
                            reference=content_edit_form.get("draft_ref"),
                            purpose="content-draft-edit",
                        )
                    raw_drafts = tenant_data.content_drafts(
                        session,
                        instagram_account_id=selected.instagram_account_id,
                    )
                    drafts = [
                        {
                            **draft,
                            "edit_ref": _content_draft_reference(
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
                            "recover_ref": _content_draft_reference(user.id, draft["id"], purpose="content-recover") if draft.get("id") else None,
                            "edit_form": (
                                content_edit_form
                                if submitted_edit_draft is not None
                                and str(draft.get("id")) == str(submitted_edit_draft.id)
                                else None
                            ),
                        }
                        for draft in raw_drafts
                    ]
            except SQLAlchemyError:
                return _workspace_unavailable()

        response = _render_page(
            "app_content.html",
            user=user,
            accounts=accounts,
            selected=selected,
            content_items=_content_payload(
                content_page.content,
                internal_prompt_access=internal_prompt_access,
            ),
            content_page=content_page,
            content_filters=tenant_data.CONTENT_FILTER_LABELS,
            selected_filter=selected_filter,
            drafts=drafts,
            content_types=CONTENT_TYPES,
            internal_prompt_access=internal_prompt_access,
            content_upload_available=content_media_is_configured(),
            content_error=content_error,
            content_form=content_form or {},
        )
        response.status_code = status
        return response

    @dashboard.get("/content")
    @_require_tenant
    def app_content(
        user,
        accounts,
        selected,
    ):
        return _render_content_page(user, accounts, selected)

    @dashboard.post("/content/drafts/create")
    @_require_tenant
    def app_content_create_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()

        session_factory = getattr(_auth_state(), "session_factory", None)
        if session_factory is None:
            return _workspace_unavailable()

        caption_value = str(request.form.get("caption") or "")
        submit_action = str(request.form.get("submit_action") or "save").strip().lower()
        content_form = {
            "media_url": str(request.form.get("media_url") or ""),
            "caption": caption_value,
            "date": str(request.form.get("date") or ""),
            "time": str(request.form.get("time") or ""),
        }
        if submit_action not in {"save", "schedule"}:
            return _safe_content_update_error()
        if not caption_value.strip() or len(caption_value.strip()) > MAX_CAPTION_LENGTH:
            return _safe_content_update_error()
        if submit_action == "schedule" and getattr(selected, "is_legacy", False):
            # Publishing a scheduled post needs a stored OAuth credential,
            # which the server-managed account never has; the scheduler
            # would mark the post failed. Drafts stay available.
            return _render_content_page(
                user,
                accounts,
                selected,
                content_error=LEGACY_SCHEDULING_UNAVAILABLE_MESSAGE,
                content_form=content_form,
                status=400,
            )

        scheduled_for = None
        if submit_action == "schedule":
            try:
                local_time = datetime.fromisoformat(
                    f"{content_form['date']}T{content_form['time']}"
                )
                scheduled_for = local_time.replace(
                    tzinfo=ZoneInfo("Africa/Casablanca")
                )
                if scheduled_for.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    raise ValueError
            except (TypeError, ValueError):
                return _render_content_page(
                    user,
                    accounts,
                    selected,
                    content_error=CONTENT_SCHEDULE_INVALID_MESSAGE,
                    content_form=content_form,
                    status=400,
                )

        uploaded_asset = None
        image_file = request.files.get("image_file")
        if image_file is not None and str(image_file.filename or "").strip():
            try:
                uploaded_asset = upload_content_image(
                    image_file,
                    instagram_account_id=selected.instagram_account_id,
                )
                media_value = uploaded_asset.url
            except ContentMediaValidationError as exc:
                message = (
                    CONTENT_IMAGE_TOO_LARGE_MESSAGE
                    if exc.code == "image_too_large"
                    else CONTENT_IMAGE_INVALID_MESSAGE
                )
                return _render_content_page(
                    user,
                    accounts,
                    selected,
                    content_error=message,
                    content_form=content_form,
                    status=400,
                )
            except ContentMediaUnavailableError:
                return _render_content_page(
                    user,
                    accounts,
                    selected,
                    content_error=CONTENT_UPLOAD_UNAVAILABLE_MESSAGE,
                    content_form=content_form,
                    status=503,
                )
            except ContentMediaProviderError:
                return _render_content_page(
                    user,
                    accounts,
                    selected,
                    content_error=CONTENT_UPLOAD_FAILED_MESSAGE,
                    content_form=content_form,
                    status=502,
                )
        else:
            try:
                media_value = public_image_url(
                    request.form.get("media_url")
                    or request.form.get("media_reference")
                )
            except ValueError:
                return _render_content_page(
                    user,
                    accounts,
                    selected,
                    content_error="Enter a public HTTPS image link without embedded credentials.",
                    content_form=content_form,
                    status=400,
                )

        metadata = {"content_type": "image"}
        if uploaded_asset is not None:
            metadata["storage"] = {
                "provider": "cloudinary",
                "asset_id": uploaded_asset.asset_id,
            }
        try:
            with session_factory.begin() as session:
                item = create_content_draft(
                    session,
                    instagram_account_id=selected.instagram_account_id,
                    content_type="image",
                    caption=caption_value,
                    media_url=media_value,
                    status="ready" if submit_action == "schedule" else "draft",
                    metadata_json=metadata,
                )
                if scheduled_for is not None:
                    schedule_content_draft(
                        session,
                        selected.instagram_account_id,
                        item.id,
                        scheduled_for,
                    )
        except (SQLAlchemyError, ValueError):
            if uploaded_asset is not None:
                delete_content_image(uploaded_asset.asset_id)
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
        if media_url_value is not None:
            try:
                media_url_value = public_image_url(media_url_value)
            except ValueError:
                return _render_content_page(
                    user,
                    accounts,
                    selected,
                    content_error="Enter a public HTTPS image link without embedded credentials.",
                    content_edit_form={
                        "draft_ref": draft_ref,
                        "media_url": str(request.form.get("media_url") or ""),
                        "caption": str(request.form.get("caption") or ""),
                    },
                    status=400,
                )

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

        asset_id = None
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
                if draft.status not in {"draft", "ready"}:
                    raise ValueError("Only unpublished drafts can be deleted.")
                asset_id = _content_storage_asset_id(draft)
                delete_content_draft(
                    session,
                    selected.instagram_account_id,
                    draft.id,
                )
        except (PermissionError, SQLAlchemyError, ValueError):
            return _safe_content_update_error()

        if asset_id:
            delete_content_image(asset_id)

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
        if getattr(selected, "is_legacy", False):
            return _render_content_page(
                user,
                accounts,
                selected,
                content_error=LEGACY_SCHEDULING_UNAVAILABLE_MESSAGE,
                status=400,
            )
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

    @dashboard.post("/content/drafts/recover")
    @_require_tenant
    def app_content_recover_draft(user, accounts, selected):
        if not validate_csrf_token(request_csrf_token()):
            return _safe_content_update_error()
        draft_ref = str(request.form.get("draft_ref") or "").strip()
        try:
            session_factory = getattr(_auth_state(), "session_factory", None)
            if session_factory is None:
                return _workspace_unavailable()
            with session_factory.begin() as session:
                draft = _resolve_owned_content_draft(session, user_id=user.id, selected_account_id=selected.instagram_account_id, reference=draft_ref, purpose="content-recover")
                if draft is None:
                    raise PermissionError("Draft not found for this account.")
                recover_failed_content_draft(session, selected.instagram_account_id, draft.id)
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
        selected_content_status = str(
            request.args.get("content_status") or "all"
        ).strip().lower()
        if selected_content_status not in {"all", "scheduled", "published", "failed"}:
            selected_content_status = "all"
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
                            display_time(item["display_time"], "en")[:10]
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
            analytics_has_activity=tenant_data.analytics_has_activity(
                data.metrics,
                data.followup_breakdown,
            ),
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
        internal_prompt_access = current_user_has_legacy_workspace_access()
        automation_error = None
        if request.method == "POST":
            if not validate_csrf_token(
                request_csrf_token()
            ):
                return _safe_automation_update_error()

            if getattr(selected, "is_legacy", False):
                # Tenant automation settings never apply to the
                # server-managed account, so never create a config row
                # the tenant scheduler could pick up without a credential.
                automation_error = LEGACY_AUTOMATION_READONLY_MESSAGE
            else:
                updated = _update_automation_config(
                    selected,
                    internal_prompt_access=internal_prompt_access,
                )

                if updated is _DATABASE_UNAVAILABLE:
                    return _workspace_unavailable()

                if updated is None:
                    return _safe_account_unavailable()

                if updated is _INVALID_DESTINATION_URL:
                    automation_error = AUTOMATION_DESTINATION_URL_ERROR_MESSAGE

                if updated is False:
                    automation_error = AUTOMATION_UPDATE_ERROR_MESSAGE

        automation = _automation_config(
            selected,
            internal_prompt_access=internal_prompt_access,
        )

        if automation is _DATABASE_UNAVAILABLE:
            return _workspace_unavailable()

        if automation is None:
            return _safe_account_unavailable()

        if automation_error is not None and not getattr(selected, "is_legacy", False):
            automation = _submitted_automation_presentation(automation)

        response = _render_page(
            "app_automations.html",
            user=user,
            accounts=accounts,
            selected=selected,
            automation=automation,
            automation_saved=request.method == "POST" and automation_error is None,
            automation_error=automation_error,
            smart_reply_tones=SMART_REPLY_TONES,
            internal_prompt_access=internal_prompt_access,
        )
        if automation_error is not None:
            response.status_code = 400
        return response

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
            is_legacy=getattr(selected, "is_legacy", False),
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
        internal_prompt_access = current_user_has_legacy_workspace_access()
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
            content=_content_payload(
                content_page.content,
                internal_prompt_access=internal_prompt_access,
            ),
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
