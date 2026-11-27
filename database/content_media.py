"""Durable, tenant-safe media storage for authenticated Content uploads."""

from __future__ import annotations

import collections
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
# Instagram Graph API v25.0 Reels limits (Meta IG User media reference,
# "Content publishing" docs): MP4/MOV, H.264 or HEVC, 3 seconds to
# 15 minutes, 300 MB maximum, width up to 1920 px, aspect ratio
# 0.01:1 to 10:1 (9:16 recommended), frame rate 23-60 fps.
MAX_CONTENT_VIDEO_BYTES = 300 * 1024 * 1024
REEL_MIN_DURATION_SECONDS = 3.0
REEL_MAX_DURATION_SECONDS = 15 * 60.0
REEL_MAX_WIDTH = 1920
REEL_MIN_ASPECT_RATIO = 0.01
REEL_MAX_ASPECT_RATIO = 10.0
_VIDEO_CODECS = frozenset({"avc1", "hvc1", "hev1"})
_HEAD_SAMPLE_BYTES = 1024 * 1024
_TAIL_SAMPLE_BYTES = 8 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_CONTENT_ASSET_PATTERN = re.compile(
    r"postenza/content/[0-9a-f]{32}/[0-9a-f]{32}\Z"
)
_JPEG_CONTENT_TYPES = {"image/jpeg", "image/jpg"}
_VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime"}
_VIDEO_EXTENSIONS = (".mp4", ".mov")


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


def _iter_boxes(data: bytes):
    """Yield (box_type, content_start, content_end) for top-level boxes."""

    offset = 0
    end = len(data)
    while offset + 8 <= end:
        size = int.from_bytes(data[offset:offset + 4], "big")
        box_type = data[offset + 4:offset + 8].decode("latin-1")
        header = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            return
        yield box_type, offset + header, offset + size
        offset += size


def _parse_mvhd(content: bytes) -> float | None:
    """Return the movie duration in seconds from an mvhd box, or None."""

    if len(content) < 20:
        return None
    version = content[0]
    if version == 1:
        if len(content) < 32:
            return None
        timescale = int.from_bytes(content[20:24], "big")
        duration = int.from_bytes(content[24:32], "big")
    else:
        timescale = int.from_bytes(content[12:16], "big")
        duration = int.from_bytes(content[16:20], "big")
    if timescale == 0:
        return None
    return duration / timescale


def _parse_tkhd(content: bytes) -> tuple[float, float] | None:
    """Return (width, height) in pixels from a tkhd box, or None."""

    version = content[0] if content else 0
    width_offset = 84 if version == 1 else 76
    if len(content) < width_offset + 8:
        return None
    width = int.from_bytes(content[width_offset:width_offset + 4], "big") / 65536
    height = int.from_bytes(content[width_offset + 4:width_offset + 8], "big") / 65536
    return width, height


def _stsd_codecs(content: bytes) -> set[str]:
    """Return the sample-entry codec fourCCs declared in an stsd box."""

    codecs: set[str] = set()
    if len(content) < 8:
        return codecs
    offset = 8
    while offset + 8 <= len(content):
        size = int.from_bytes(content[offset:offset + 4], "big")
        if size < 8 or offset + size > len(content):
            break
        codecs.add(content[offset + 4:offset + 8].decode("latin-1"))
        offset += size
    return codecs


def _probe_video_sample(sample: bytes) -> dict[str, object] | None:
    """Extract duration, dimensions and video codecs from one MP4/MOV sample.

    Returns None when the sample holds no complete ``moov`` box (the box
    usually sits at the front for fast-start files and at the end otherwise,
    so callers probe both the head and the tail of the file).
    """

    moov = None
    for box_type, start, end in _iter_boxes(sample):
        if box_type == "moov":
            moov = sample[start:end]
            break
    if moov is None:
        return None
    duration: float | None = None
    width: float | None = None
    height: float | None = None
    video_codecs: set[str] = set()
    saw_video_track = False
    for box_type, start, end in _iter_boxes(moov):
        if box_type == "mvhd":
            duration = _parse_mvhd(moov[start:end])
        elif box_type == "trak":
            trak = moov[start:end]
            handler: str | None = None
            tkhd: bytes | None = None
            codecs: set[str] = set()
            for inner_type, inner_start, inner_end in _iter_boxes(trak):
                if inner_type == "tkhd":
                    tkhd = trak[inner_start:inner_end]
                elif inner_type == "mdia":
                    mdia = trak[inner_start:inner_end]
                    for mdia_type, mdia_start, mdia_end in _iter_boxes(mdia):
                        if mdia_type == "hdlr":
                            hdlr = mdia[mdia_start:mdia_end]
                            if len(hdlr) >= 12:
                                handler = hdlr[8:12].decode("latin-1")
                        elif mdia_type == "minf":
                            minf = mdia[mdia_start:mdia_end]
                            for minf_type, minf_start, minf_end in _iter_boxes(minf):
                                if minf_type == "stbl":
                                    stbl = minf[minf_start:minf_end]
                                    for stbl_type, stbl_start, stbl_end in _iter_boxes(stbl):
                                        if stbl_type == "stsd":
                                            codecs = _stsd_codecs(stbl[stbl_start:stbl_end])
            if handler == "vide":
                saw_video_track = True
                if tkhd is not None:
                    dims = _parse_tkhd(tkhd)
                    if dims is not None:
                        width, height = dims
                video_codecs |= codecs
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "video_codecs": video_codecs,
        "saw_video_track": saw_video_track,
    }


def _validate_reel_video(probe: dict[str, object] | None) -> None:
    """Reject a probed video that Meta would refuse, with a distinct code."""

    if probe is None or probe.get("duration") is None:
        raise ContentMediaValidationError("video_unreadable")
    if not probe.get("saw_video_track") or not (
        set(probe.get("video_codecs") or set()) & _VIDEO_CODECS
    ):
        raise ContentMediaValidationError("video_codec_unsupported")
    duration = float(probe["duration"])  # type: ignore[arg-type]
    if duration < REEL_MIN_DURATION_SECONDS:
        raise ContentMediaValidationError("video_too_short")
    if duration > REEL_MAX_DURATION_SECONDS:
        raise ContentMediaValidationError("video_too_long")
    width = probe.get("width")
    height = probe.get("height")
    if (
        not isinstance(width, (int, float))
        or not isinstance(height, (int, float))
        or width <= 0
        or height <= 0
    ):
        raise ContentMediaValidationError("video_unreadable")
    if width > REEL_MAX_WIDTH:
        raise ContentMediaValidationError("video_dimensions_invalid")
    aspect = width / height
    if aspect < REEL_MIN_ASPECT_RATIO or aspect > REEL_MAX_ASPECT_RATIO:
        raise ContentMediaValidationError("video_dimensions_invalid")


def _read_video_stream(upload):
    """Stream a video upload once, counting bytes and sampling head/tail.

    Only the first 1 MB and the last 8 MB are retained: the ``moov`` box
    lives in one of those two places, and the rest of the file is never
    held in memory. Returns the (rewound) stream plus the two samples.
    """

    filename = str(getattr(upload, "filename", "") or "").strip().lower()
    content_type = str(getattr(upload, "content_type", "") or "").strip().lower()
    if content_type not in _VIDEO_CONTENT_TYPES or not filename.endswith(_VIDEO_EXTENSIONS):
        raise ContentMediaValidationError("invalid_video")
    stream = getattr(upload, "stream", upload)
    head = bytearray()
    tail: collections.deque[bytes] = collections.deque()
    tail_size = 0
    total = 0
    try:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONTENT_VIDEO_BYTES:
                raise ContentMediaValidationError("video_too_large")
            if len(head) < _HEAD_SAMPLE_BYTES:
                head += chunk[: _HEAD_SAMPLE_BYTES - len(head)]
            tail.append(chunk)
            tail_size += len(chunk)
            while tail_size > _TAIL_SAMPLE_BYTES:
                tail_size -= len(tail.popleft())
    except ContentMediaValidationError:
        raise
    except Exception as exc:
        raise ContentMediaValidationError("invalid_video") from exc
    if total == 0:
        raise ContentMediaValidationError("invalid_video")
    try:
        stream.seek(0)
    except Exception as exc:
        raise ContentMediaValidationError("invalid_video") from exc
    return stream, bytes(head), b"".join(tail)


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


def upload_content_video(upload, *, instagram_account_id: object) -> ContentMediaAsset:
    """Validate a Reel video, then store it on Cloudinary as a public video.

    The file is probed (MP4/MOV structure, H.264/HEVC codec, duration,
    dimensions) before any provider call, so an invalid file is rejected
    here with a clear code instead of failing silently at publish time.
    The upload stream is rewound and streamed to Cloudinary; the file is
    never fully held in memory. The returned URL is a public HTTPS link,
    which is what the Instagram Graph API requires for ``video_url``.
    """

    configuration = _configuration()
    if configuration is None:
        raise ContentMediaUnavailableError("upload_unavailable")
    cloud_name, api_key, api_secret = configuration
    stream, head, tail = _read_video_stream(upload)
    probe = _probe_video_sample(head)
    if probe is None or probe.get("duration") is None:
        tail_probe = _probe_video_sample(tail)
        if tail_probe is not None:
            probe = tail_probe
    _validate_reel_video(probe)
    filename = str(getattr(upload, "filename", "") or "").strip().lower()
    video_format = "mov" if filename.endswith(".mov") else "mp4"
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
            f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload",
            data=data,
            files={"file": (f"content.{video_format}", stream, f"video/{'quicktime' if video_format == 'mov' else 'mp4'}")},
            timeout=300,
        )
        if not response.ok:
            raise ContentMediaProviderError("upload_failed")
        payload = response.json()
    except ContentMediaProviderError:
        raise
    except (requests.RequestException, TypeError, ValueError) as exc:
        raise ContentMediaProviderError("upload_failed") from exc
    if not isinstance(payload, dict) or str(payload.get("public_id") or "") != asset_id:
        delete_content_video(asset_id)
        raise ContentMediaProviderError("upload_failed")
    try:
        media_url = _safe_https_url(payload.get("secure_url"))
    except ContentMediaProviderError:
        delete_content_video(asset_id)
        raise
    return ContentMediaAsset(
        url=media_url,
        asset_id=asset_id,
    )


def _delete_content_asset(asset_id: object, resource_type: str) -> bool:
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
            f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/destroy",
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


def delete_content_image(asset_id: object) -> bool:
    return _delete_content_asset(asset_id, "image")


def delete_content_video(asset_id: object) -> bool:
    return _delete_content_asset(asset_id, "video")


__all__ = [
    "MAX_CONTENT_IMAGE_BYTES",
    "MAX_CONTENT_VIDEO_BYTES",
    "REEL_MAX_DURATION_SECONDS",
    "REEL_MAX_WIDTH",
    "REEL_MIN_DURATION_SECONDS",
    "ContentMediaAsset",
    "ContentMediaProviderError",
    "ContentMediaUnavailableError",
    "ContentMediaValidationError",
    "content_media_is_configured",
    "delete_content_image",
    "delete_content_video",
    "upload_content_image",
    "upload_content_video",
]
