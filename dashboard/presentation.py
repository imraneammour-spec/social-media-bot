"""Tenant UI helpers. No remote fetching, storage, or publishing side effects."""
import ipaddress
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .i18n import normalize_locale, translate


def public_image_url(value):
    """Validate URL syntax at the form boundary; not a server-side fetch policy.

    DNS and image availability are deliberately not probed here. Instagram still
    validates the remote media at publication time.
    """
    value = str(value or "").strip()
    if len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("invalid_image_url")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        valid_port = parts.port is None or 0 < parts.port <= 65535
    except ValueError:
        raise ValueError("invalid_image_url") from None
    if (parts.scheme != "https" or not host or not valid_port or parts.username is not None
            or parts.password is not None or "." not in host
            or host.endswith((".localhost", ".local", ".internal"))):
        raise ValueError("invalid_image_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("invalid_image_url")
    return value


def display_time(value, locale="en"):
    """Format persisted UTC timestamps for the existing Casablanca scheduling UI."""
    try:
        date = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        date = date.astimezone(ZoneInfo("Africa/Casablanca"))
    except (ValueError, TypeError):
        return translate(locale, "Date unavailable")
    return date.strftime("%Y-%m-%d %H:%M" if normalize_locale(locale) == "en" else "%d/%m/%Y %H:%M")
