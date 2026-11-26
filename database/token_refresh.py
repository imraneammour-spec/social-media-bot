"""Refresh unexpired Instagram Login tokens without logging credentials."""
import logging
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import select

from .billing import utc
from .credentials import credential_keyring_from_environment, decrypt_access_token, encrypt_access_token
from .models import InstagramAccount, InstagramCredential

LOGGER = logging.getLogger(__name__)


def refresh_expiring_tokens(factory, *, now=None, get=requests.get):
    now = now or datetime.now(timezone.utc)
    with factory() as db:
        candidates = db.scalars(select(InstagramCredential).join(InstagramAccount).where(
            InstagramAccount.connection_status == "connected",
            InstagramCredential.token_status == "valid",
            InstagramCredential.expires_at.is_not(None),
            InstagramCredential.expires_at < now + timedelta(days=7),
        ).limit(100)).all()
    if not candidates:
        return 0
    keyring = credential_keyring_from_environment()
    refreshed = 0
    for snapshot in candidates:
        try:
            if utc(snapshot.expires_at) <= now:
                with factory.begin() as db:
                    account = db.scalar(select(InstagramAccount).where(
                        InstagramAccount.id == snapshot.instagram_account_id).with_for_update())
                    credential = db.get(InstagramCredential, snapshot.id)
                    if account and credential and account.connection_status == "connected" and credential.token_ciphertext == snapshot.token_ciphertext and credential.expires_at and utc(credential.expires_at) <= now:
                        credential.token_status = "expired"
                        account.connection_status = "reconnect_required"
                        account.last_connection_error_code = "token_expired"
                continue
            issued = snapshot.last_refreshed_at or snapshot.issued_at
            if issued is None or utc(issued) > now - timedelta(hours=24):
                continue
            token = decrypt_access_token(snapshot.token_ciphertext, snapshot.encryption_key_id, keyring)
            response = get("https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=(5, 20))
            response.raise_for_status()
            payload = response.json()
            duration = payload.get("expires_in")
            new_token = payload.get("access_token")
            if type(duration) is not int or not 0 < duration <= 366 * 86400 or not isinstance(new_token, str) or not new_token:
                raise ValueError("Invalid refresh response.")
            encrypted = encrypt_access_token(new_token, keyring)
            with factory.begin() as db:
                account = db.scalar(select(InstagramAccount).where(InstagramAccount.id == snapshot.instagram_account_id).with_for_update())
                credential = db.get(InstagramCredential, snapshot.id)
                if not account or not credential or account.connection_status != "connected" or credential.token_ciphertext != snapshot.token_ciphertext:
                    continue  # An owner disconnected or reconnected while the provider request ran.
                credential.token_ciphertext = encrypted.token_ciphertext
                credential.encryption_key_id = encrypted.encryption_key_id
                credential.expires_at = now + timedelta(seconds=duration)
                credential.last_refreshed_at = now
                account.token_expires_at = credential.expires_at
                refreshed += 1
        except Exception as error:
            LOGGER.warning("instagram_refresh_failed error_type=%s", type(error).__name__)
    return refreshed
