"""Durable, tenant-safe media storage for authenticated Content uploads."""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
import warnings
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit

import requests
from PIL import Image, UnidentifiedImageError


MAX_CONTENT_IMAGE_BYTES = 8 * 1024 * 1024
_CONTENT_ASSET_PATTERN = re.compile(
    r"postenza/content/[0-9a-f]{32}/[0-9a-f]{32}\Z"
)
_JPEG_CONTENT_TYPES = {"image/jpeg", "image/jpg"}


@dataclass(frozen=True)
class ContentMediaAsset:
    url: str
    asset_id: str


class ContentMediaError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ContentMediaUnavailableError(ContentMediaError):
    pass


class ContentMediaValidationError(ContentMediaError):
    pass


class ContentMediaProviderError(ContentMediaError):
    pass


def content_media_is_configured() -> bool:
    return bool(_configuration())


def _configuration() -> tuple[str, str, str] | None:
    cloud_name = str(os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
    api_key = str(os.getenv("CLOUDINARY_API_KEY") or "").strip()
    api_secret = str(os.getenv("CLOUDINARY_API_SECRET") or "").strip()
    if not cloud_name or not api_key or not api_secret:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cloud_name):
        return None
    return cloud_name, api_key, api_secret


def _signature(parameters: dict[str, object], api_secret: str) -> str:
    signed = "&".join(
        f"{key}={parameters[key]}" for key in sorted(parameters)
    )
    return hashlib.sha1(f"{signed}{api_secret}".encode("utf-8")).hexdigest()


def _validated_jpeg(upload) -> bytes:
    filename = str(getattr(upload, "filename", "") or "").strip().lower()
    content_type = str(getattr(upload, "content_type", "") or "").strip().lower()
    if content_type not in _JPEG_CONTENT_TYPES or not filename.endswith((".jpg", ".jpeg")):
        raise ContentMediaValidationError("invalid_jpeg")
    stream = getattr(upload, "stream", upload)
    try:
        image_bytes = stream.read(MAX_CONTENT_IMAGE_BYTES + 1)
    except Exception as exc:
        raise ContentMediaValidationError("invalid_jpeg") from exc
    if len(image_bytes) > MAX_CONTENT_IMAGE_BYTES:
        raise ContentMediaValidationError("image_too_large")
    if not image_bytes:
        raise ContentMediaValidationError("invalid_jpeg")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as image:
                if image.format != "JPEG":
                    raise ContentMediaValidationError("invalid_jpeg")
                image.verify()
            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
    except ContentMediaValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError, ValueError) as exc:
        raise ContentMediaValidationError("invalid_jpeg") from exc
    return image_bytes


def _safe_https_url(value: object) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
        valid_port = parsed.port is None or 0 < parsed.port <= 65535
    except ValueError:
        valid_port = False
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not valid_port
    ):
        raise ContentMediaProviderError("upload_failed")
    return candidate


def upload_content_image(upload, *, instagram_account_id: object) -> ContentMediaAsset:
    configuration = _configuration()
    if configuration is None:
        raise ContentMediaUnavailableError("upload_unavailable")
    cloud_name, api_key, api_secret = configuration
    image_bytes = _validated_jpeg(upload)
    account_scope = uuid.UUID(str(instagram_account_id)).hex
    asset_id = f"postenza/content/{account_scope}/{uuid.uuid4().hex}"
    signed_parameters = {
        "public_id": asset_id,
        "timestamp": int(time.time()),
    }
    data = {
        **signed_parameters,
        "api_key": api_key,
        "signature": _signature(signed_parameters, api_secret),
    }
    try:
        response = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
            data=data,
            files={"file": ("content.jpg", image_bytes, "image/jpeg")},
            timeout=60,
        )
        if not response.ok:
            raise ContentMediaProviderError("upload_failed")
        payload = response.json()
    except ContentMediaProviderError:
        raise
    except (requests.RequestException, TypeError, ValueError) as exc:
        raise ContentMediaProviderError("upload_failed") from exc
    if not isinstance(payload, dict) or str(payload.get("public_id") or "") != asset_id:
        delete_content_image(asset_id)
        raise ContentMediaProviderError("upload_failed")
    try:
        media_url = _safe_https_url(payload.get("secure_url"))
    except ContentMediaProviderError:
        delete_content_image(asset_id)
        raise
    return ContentMediaAsset(
        url=media_url,
        asset_id=asset_id,
    )


def delete_content_image(asset_id: object) -> bool:
    candidate = str(asset_id or "").strip()
    configuration = _configuration()
    if configuration is None or not _CONTENT_ASSET_PATTERN.fullmatch(candidate):
        return False
    cloud_name, api_key, api_secret = configuration
    signed_parameters = {
        "public_id": candidate,
        "timestamp": int(time.time()),
    }
    try:
        response = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/image/destroy",
            data={
                **signed_parameters,
                "api_key": api_key,
                "signature": _signature(signed_parameters, api_secret),
            },
            timeout=30,
        )
        if not response.ok:
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("result") in {"ok", "not found"}
    except (requests.RequestException, TypeError, ValueError):
        return False


__all__ = [
    "MAX_CONTENT_IMAGE_BYTES",
    "ContentMediaAsset",
    "ContentMediaProviderError",
    "ContentMediaUnavailableError",
    "ContentMediaValidationError",
    "content_media_is_configured",
    "delete_content_image",
    "upload_content_image",
]
