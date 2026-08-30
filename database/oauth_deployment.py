"""Non-secret OAuth deployment readiness for tenant presentation."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .credentials import CredentialConfigurationError, credential_keyring_from_environment
from .oauth_capability import oauth_engagement_enabled_from_environment


@dataclass(frozen=True)
class OAuthDeploymentReadiness:
    state: str


def oauth_deployment_readiness(environment=None) -> OAuthDeploymentReadiness:
    source = os.environ if environment is None else environment
    missing = []
    if not oauth_engagement_enabled_from_environment(source):
        missing.append("engagement_disabled")
    try:
        parsed = urlsplit(str(source.get("PUBLIC_BASE_URL", "") or ""))
        public_ready = parsed.scheme == "https" and bool(parsed.netloc) and not parsed.query and not parsed.fragment
    except (TypeError, ValueError):
        public_ready = False
    if not public_ready:
        missing.append("public_base_unavailable")
    try:
        credential_keyring_from_environment(source)
    except CredentialConfigurationError:
        missing.append("credential_keyring_unavailable")
    return OAuthDeploymentReadiness("ready" if not missing else missing[0] if len(missing) == 1 else "multiple_prerequisites_unavailable")
