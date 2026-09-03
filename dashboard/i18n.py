"""Small session-backed UI localization helpers for the tenant dashboard."""

from __future__ import annotations

SUPPORTED_LOCALES = ("en", "fr", "ar")
DEFAULT_LOCALE = "en"
LOCALE_SESSION_KEY = "sociora_locale"

LOCALE_META = {
    "en": {"label": "English", "direction": "ltr"},
    "fr": {"label": "Français", "direction": "ltr"},
    "ar": {"label": "العربية", "direction": "rtl"},
}

TRANSLATIONS = {
    "en": {
        "product_name": "Postenza",
        "overview": "Overview", "content": "Content", "calendar": "Calendar",
        "engagement": "Engagement", "automation": "Automation", "instagram": "Instagram",
        "sign_out": "Sign out", "instagram_workspace": "Instagram workspace",
        "account": "Instagram account", "language": "Language",
        "automation_on": "Automation is on", "automation_off": "Automation is off",
        "social_tagline": "Social media, on autopilot.",
    },
    "fr": {
        "product_name": "Postenza",
        "overview": "Vue d’ensemble", "content": "Contenu", "calendar": "Calendrier",
        "engagement": "Interactions", "automation": "Automatisation", "instagram": "Instagram",
        "sign_out": "Se déconnecter", "instagram_workspace": "Espace Instagram",
        "account": "Compte Instagram", "language": "Langue",
        "automation_on": "L’automatisation est activée", "automation_off": "L’automatisation est désactivée",
        "social_tagline": "Les réseaux sociaux en pilote automatique.",
    },
    "ar": {
        "product_name": "Postenza",
        "overview": "نظرة عامة", "content": "المحتوى", "calendar": "التقويم",
        "engagement": "التفاعل", "automation": "الأتمتة", "instagram": "إنستغرام",
        "sign_out": "تسجيل الخروج", "instagram_workspace": "مساحة عمل إنستغرام",
        "account": "حساب إنستغرام", "language": "اللغة",
        "automation_on": "الأتمتة مفعّلة", "automation_off": "الأتمتة متوقفة",
        "social_tagline": "وسائل التواصل الاجتماعي، على الطيار الآلي.",
    },
}


def normalize_locale(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in SUPPORTED_LOCALES else DEFAULT_LOCALE


def locale_context(value: object) -> dict[str, object]:
    code = normalize_locale(value)
    return {"code": code, **LOCALE_META[code]}


def translate(locale: object, key: object) -> str:
    normalized = normalize_locale(locale)
    message_key = str(key or "")
    return TRANSLATIONS[normalized].get(message_key, TRANSLATIONS[DEFAULT_LOCALE].get(message_key, message_key))
