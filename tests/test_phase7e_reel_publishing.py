"""Phase 7E — Instagram Reels publishing through the official Graph API v25.0.

Covers: Reel draft validation, video upload validation (pure-Python MP4
probe — no ffmpeg on the server), the two-step REELS container flow with
its longer readiness poll, the Professional-account guard, dispatch of
scheduled Reels alongside images, and the rule that an invalid file is
rejected with a clear message instead of failing silently.
"""

import struct
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.datastructures import FileStorage

from database.account_runtime import InstagramAccountRuntime
from database.content import CONTENT_TYPES, create_content_draft, normalize_content_type, update_content_draft
from database.content_media import (
    ContentMediaValidationError,
    upload_content_video,
)
from database.models import Base, InstagramAccount, InstagramContentItem, User
from dashboard.presentation import public_video_url


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _minimal_mp4(
    *,
    duration_ms: int = 5000,
    timescale: int = 1000,
    width: int = 720,
    height: int = 1280,
    codec: bytes = b"avc1",
    moov_at_end: bool = False,
) -> bytes:
    """Build a structurally valid MP4: ftyp + moov(mvhd + trak(tkhd + mdia))."""

    mvhd = _box(
        b"mvhd",
        struct.pack(">I", 0)
        + struct.pack(">IIII", 0, 0, timescale, duration_ms)
        + b"\x00" * 80,
    )
    tkhd = _box(
        b"tkhd",
        struct.pack(">I", 0x7)
        + struct.pack(">IIIII", 0, 0, 1, 0, duration_ms)
        + b"\x00" * 8
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + b"\x00" * 36
        + struct.pack(">II", width << 16, height << 16),
    )
    hdlr = _box(
        b"hdlr",
        struct.pack(">I", 0) + struct.pack(">I", 0) + b"vide" + b"\x00" * 12 + b"\x00",
    )
    stsd = _box(b"stsd", struct.pack(">II", 0, 1) + _box(codec, b"\x00" * 78))
    mdia = _box(b"mdia", hdlr + _box(b"minf", _box(b"stbl", stsd)))
    moov = _box(b"moov", mvhd + _box(b"trak", tkhd + mdia))
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    if moov_at_end:
        # A large free box pushes moov past the 1 MB head sample, so the
        # probe must find it in the tail sample instead.
        return ftyp + _box(b"free", b"\x00" * (1024 * 1024 + 64)) + moov
    return ftyp + moov


def _video_upload(data: bytes, *, filename="reel.mp4", content_type="video/mp4") -> FileStorage:
    return FileStorage(stream=BytesIO(data), filename=filename, content_type=content_type)


class _ProviderResponse:
    def __init__(self, *, ok=True, status_code=None, payload=None):
        self.ok = ok
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ReelDraftValidationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'phase7e.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            owner = User(email="reel-a@example.test")
            session.add(owner)
            session.flush()
            self.account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="73000000000000001",
                username="reel_a",
                connection_status="connected",
            )
            session.add(self.account)
            session.flush()
            self.account_id = self.account.id

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_reel_is_a_supported_content_type_and_plain_video_is_not(self):
        self.assertEqual(CONTENT_TYPES, ("image", "reel"))
        self.assertEqual(normalize_content_type("Reel"), "reel")
        for unsupported in ("video", "carousel", "story"):
            with self.subTest(content_type=unsupported), self.assertRaises(ValueError) as error:
                normalize_content_type(unsupported)
            self.assertIn("Only image posts and Reels are supported", str(error.exception))

    def test_reel_draft_requires_an_mp4_or_mov_link(self):
        with self.Session.begin() as session:
            item = create_content_draft(
                session,
                instagram_account_id=self.account_id,
                caption="Reel draft",
                content_type="reel",
                media_url="https://videos.example.com/reel.mp4",
                cover_url="https://images.example.com/cover.jpg",
            )
        self.assertEqual(item.content_type, "reel")
        self.assertEqual(item.media_url, "https://videos.example.com/reel.mp4")
        self.assertEqual(item.cover_url, "https://images.example.com/cover.jpg")

        for bad_url in (
            "https://images.example.com/reel.jpg",
            "http://videos.example.com/reel.mp4",
            "https://videos.example.com/reel",
        ):
            with self.subTest(media_url=bad_url), self.Session.begin() as session, self.assertRaises(ValueError):
                create_content_draft(
                    session,
                    instagram_account_id=self.account_id,
                    caption="Bad reel",
                    content_type="reel",
                    media_url=bad_url,
                )

    def test_reel_cover_must_be_a_jpeg_link(self):
        with self.Session.begin() as session, self.assertRaises(ValueError):
            create_content_draft(
                session,
                instagram_account_id=self.account_id,
                caption="Bad cover",
                content_type="reel",
                media_url="https://videos.example.com/reel.mp4",
                cover_url="https://images.example.com/cover.png",
            )

    def test_reel_draft_without_cover_keeps_no_cover(self):
        with self.Session.begin() as session:
            item = create_content_draft(
                session,
                instagram_account_id=self.account_id,
                caption="No cover",
                content_type="reel",
                media_url="https://videos.example.com/reel.mov",
            )
        self.assertIsNone(item.cover_url)

    def test_changing_an_image_draft_to_reel_requires_a_video_link(self):
        with self.Session.begin() as session:
            draft = create_content_draft(
                session,
                instagram_account_id=self.account_id,
                caption="Image first",
                content_type="image",
                media_url="https://images.example.com/photo.jpg",
            )
            with self.assertRaises(ValueError):
                update_content_draft(
                    session,
                    self.account_id,
                    draft.id,
                    content_type="reel",
                )


class ReelVideoUploadTest(unittest.TestCase):
    def setUp(self):
        self.configuration = {
            "CLOUDINARY_CLOUD_NAME": "postenza-test",
            "CLOUDINARY_API_KEY": "public-api-key",
            "CLOUDINARY_API_SECRET": "private-api-secret",
        }
        self.account_id = uuid.UUID("11111111-2222-3333-4444-555555555555")

    def _cloudinary_response(self, asset_id, secure_url):
        return _ProviderResponse(payload={"public_id": asset_id, "secure_url": secure_url})

    def test_valid_mp4_uploads_as_video_and_returns_public_url(self):
        asset_id = "postenza/content/11111111222233334444555555555555/0123456789abcdef0123456789abcdef"
        secure_url = "https://res.cloudinary.com/postenza-test/video/upload/postenza/content/11111111222233334444555555555555/0123456789abcdef0123456789abcdef.mp4"
        with patch.dict("os.environ", self.configuration), patch(
            "database.content_media.uuid.uuid4",
            return_value=type("Uuid", (), {"hex": "0123456789abcdef0123456789abcdef"})(),
        ), patch(
            "database.content_media.requests.post",
            return_value=self._cloudinary_response(asset_id, secure_url),
        ) as post:
            asset = upload_content_video(
                _video_upload(_minimal_mp4()),
                instagram_account_id=self.account_id,
            )
        self.assertEqual(asset.url, secure_url)
        self.assertEqual(asset.asset_id, asset_id)
        self.assertIn("/video/upload", post.call_args.args[0])

    def test_moov_at_end_of_file_is_found_in_tail_sample(self):
        asset_id = "postenza/content/11111111222233334444555555555555/abcdef0123456789abcdef0123456789"
        secure_url = "https://res.cloudinary.com/postenza-test/video/upload/x.mp4"
        with patch.dict("os.environ", self.configuration), patch(
            "database.content_media.uuid.uuid4",
            return_value=type("Uuid", (), {"hex": "abcdef0123456789abcdef0123456789"})(),
        ), patch(
            "database.content_media.requests.post",
            return_value=self._cloudinary_response(asset_id, secure_url),
        ):
            asset = upload_content_video(
                _video_upload(_minimal_mp4(moov_at_end=True)),
                instagram_account_id=self.account_id,
            )
        self.assertEqual(asset.asset_id, asset_id)

    def test_invalid_video_files_are_rejected_with_distinct_codes(self):
        cases = (
            (_video_upload(b"not a video at all"), "video_unreadable"),
            (_video_upload(_minimal_mp4(duration_ms=1000)), "video_too_short"),
            (_video_upload(_minimal_mp4(duration_ms=16 * 60 * 1000)), "video_too_long"),
            (_video_upload(_minimal_mp4(width=3840, height=2160)), "video_dimensions_invalid"),
            (_video_upload(_minimal_mp4(codec=b"mp4v")), "video_codec_unsupported"),
            (_video_upload(_minimal_mp4(), filename="reel.avi", content_type="video/avi"), "invalid_video"),
            (_video_upload(b"", filename="reel.mp4"), "invalid_video"),
        )
        for upload, expected_code in cases:
            with self.subTest(expected_code=expected_code), patch.dict("os.environ", self.configuration):
                with self.assertRaises(ContentMediaValidationError) as error:
                    upload_content_video(upload, instagram_account_id=self.account_id)
                self.assertEqual(error.exception.code, expected_code)

    def test_oversize_video_is_rejected_before_calling_provider(self):
        with patch.dict("os.environ", self.configuration), patch(
            "database.content_media.MAX_CONTENT_VIDEO_BYTES", 10
        ), patch("database.content_media.requests.post") as post:
            with self.assertRaises(ContentMediaValidationError) as error:
                upload_content_video(
                    _video_upload(_minimal_mp4()),
                    instagram_account_id=self.account_id,
                )
        self.assertEqual(error.exception.code, "video_too_large")
        post.assert_not_called()


class PublicVideoUrlTest(unittest.TestCase):
    def test_valid_mp4_and_mov_links_pass(self):
        self.assertEqual(
            public_video_url("https://videos.example.com/reel.mp4"),
            "https://videos.example.com/reel.mp4",
        )
        self.assertEqual(
            public_video_url("https://videos.example.com/reel.MOV"),
            "https://videos.example.com/reel.MOV",
        )

    def test_non_video_or_unsafe_links_fail(self):
        for bad_url in (
            "https://images.example.com/photo.jpg",
            "http://videos.example.com/reel.mp4",
            "https://videos.example.com/reel",
            "https://user:secret@videos.example.com/reel.mp4",
            "https://videos.example.com/reel.mp4 with space",
        ):
            with self.subTest(url=bad_url), self.assertRaises(ValueError):
                public_video_url(bad_url)


class ReelPublishingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'phase7e-publish.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        with self.Session.begin() as session:
            owner = User(email="reel-publish@example.test")
            session.add(owner)
            session.flush()
            self.account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="74000000000000001",
                username="reel_publish",
                connection_status="connected",
            )
            session.add(self.account)
            session.flush()
            self.account_id = self.account.id

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _api(self):
        import importlib

        return importlib.import_module("database.content_publishing")

    def _scheduled(self, *, content_type="reel", media_url="https://videos.example.com/reel.mp4"):
        with self.Session.begin() as session:
            item = create_content_draft(
                session,
                instagram_account_id=self.account_id,
                caption="Scheduled reel",
                content_type=content_type,
                media_url=media_url,
            )
            item.status = "scheduled"
            item.scheduled_for = self.now - timedelta(minutes=1)
            return item.id

    def _item(self, item_id):
        with self.Session() as session:
            return session.get(InstagramContentItem, item_id)

    def _runtime(self):
        return InstagramAccountRuntime(
            self.account_id, self.account.instagram_user_id, "oauth", "test-token-must-not-persist"
        )

    def test_successful_reel_publish_uses_reels_container_and_cover(self):
        api = self._api()
        item_id = self._scheduled()
        with self.Session.begin() as session:
            claimed = api.claim_due_scheduled_content(session, now=self.now, limit=1)[0]
        claimed = api.ClaimedScheduledContent(
            id=claimed.id,
            instagram_account_id=claimed.instagram_account_id,
            instagram_user_id=claimed.instagram_user_id,
            content_type=claimed.content_type,
            caption=claimed.caption,
            media_url=claimed.media_url,
            cover_url="https://images.example.com/cover.jpg",
        )
        account_type = _ProviderResponse(payload={"account_type": "CREATOR"})
        finished = _ProviderResponse(payload={"status_code": "FINISHED"})
        with patch(
            "database.content_publishing.requests.post",
            side_effect=(
                _ProviderResponse(payload={"id": "reel-container"}),
                _ProviderResponse(payload={"id": "published-reel"}),
            ),
        ) as post, patch(
            "database.content_publishing.requests.get",
            side_effect=(account_type, finished),
        ) as get, patch("time.sleep"):
            provider_media_id = api.publish_instagram_reel(self._runtime(), claimed)

        self.assertEqual(provider_media_id, "published-reel")
        container_data = post.call_args_list[0].kwargs["data"]
        self.assertEqual(container_data["media_type"], "REELS")
        self.assertEqual(container_data["video_url"], "https://videos.example.com/reel.mp4")
        self.assertEqual(container_data["caption"], "Scheduled reel")
        self.assertEqual(container_data["share_to_feed"], "true")
        self.assertEqual(container_data["cover_url"], "https://images.example.com/cover.jpg")
        self.assertIn("media_publish", post.call_args_list[1].args[0])
        self.assertEqual(post.call_args_list[1].kwargs["data"], {"creation_id": "reel-container"})
        self.assertEqual(get.call_count, 2)
        self.assertIn("account_type", get.call_args_list[0].kwargs["params"]["fields"])

    def test_reel_without_cover_omits_cover_url(self):
        api = self._api()
        item_id = self._scheduled()
        with self.Session.begin() as session:
            claimed = api.claim_due_scheduled_content(session, now=self.now, limit=1)[0]
        with patch(
            "database.content_publishing.requests.post",
            side_effect=(
                _ProviderResponse(payload={"id": "reel-container"}),
                _ProviderResponse(payload={"id": "published-reel"}),
            ),
        ) as post, patch(
            "database.content_publishing.requests.get",
            side_effect=(
                _ProviderResponse(payload={"account_type": "BUSINESS"}),
                _ProviderResponse(payload={"status_code": "FINISHED"}),
            ),
        ), patch("time.sleep"):
            api.publish_instagram_reel(self._runtime(), claimed)
        self.assertNotIn("cover_url", post.call_args_list[0].kwargs["data"])

    def test_personal_account_is_rejected_before_any_container_is_created(self):
        api = self._api()
        item_id = self._scheduled()
        with self.Session.begin() as session:
            claimed = api.claim_due_scheduled_content(session, now=self.now, limit=1)[0]
        with patch(
            "database.content_publishing.requests.post",
        ) as post, patch(
            "database.content_publishing.requests.get",
            return_value=_ProviderResponse(payload={"account_type": "PERSONAL"}),
        ):
            with self.assertRaises(api.InstagramPublishError) as error:
                api.publish_instagram_reel(self._runtime(), claimed)
        self.assertEqual(error.exception.code, "instagram_account_not_professional")
        self.assertFalse(error.exception.request_may_have_been_sent)
        post.assert_not_called()

    def test_reel_container_polled_with_longer_budget_before_publish(self):
        api = self._api()
        item_id = self._scheduled()
        in_progress = _ProviderResponse(payload={"status_code": "IN_PROGRESS"})
        with patch(
            "database.content_publishing.requests.post",
            return_value=_ProviderResponse(payload={"id": "reel-waiting"}),
        ) as post, patch(
            "database.content_publishing.requests.get",
            return_value=in_progress,
        ) as get, patch("time.sleep") as sleep:
            result = api.execute_due_scheduled_content(
                self.Session,
                now=self.now,
                runtime_resolver=lambda *args, **kwargs: self._runtime(),
            )

        item = self._item(item_id)
        self.assertEqual((result.claimed, result.published, result.failed), (1, 0, 1))
        # One account-type read plus the full Reel poll budget of 30.
        self.assertEqual(get.call_count, 31)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(item.publish_error_code, "provider_container_not_ready")
        self.assertFalse(item.provider_request_may_have_been_sent)
        self.assertEqual(sleep.call_count, 29)
        self.assertEqual(sleep.call_args_list[0].args, (10,))

    def test_execute_dispatches_reels_and_images_to_their_publishers(self):
        api = self._api()
        reel_id = self._scheduled(content_type="reel")
        image_id = self._scheduled(content_type="image", media_url="https://images.example.com/photo.jpg")
        calls = []

        def image_publisher(runtime, item):
            calls.append(("image", item.id))
            return "provider-media-image"

        def reel_publisher(runtime, item):
            calls.append(("reel", item.id))
            return "provider-media-reel"

        result = api.execute_due_scheduled_content(
            self.Session,
            now=self.now,
            runtime_resolver=lambda *args, **kwargs: self._runtime(),
            publisher=image_publisher,
            reel_publisher=reel_publisher,
        )
        self.assertEqual((result.claimed, result.published, result.failed), (2, 2, 0))
        self.assertEqual(sorted(calls), [("image", image_id), ("reel", reel_id)])
        self.assertEqual(self._item(reel_id).provider_media_id, "provider-media-reel")
        self.assertEqual(self._item(image_id).provider_media_id, "provider-media-image")

    def test_legacy_video_row_still_fails_explicitly_without_provider_calls(self):
        api = self._api()
        with self.Session.begin() as session:
            legacy = InstagramContentItem(
                instagram_account_id=self.account_id,
                status="scheduled",
                content_type="video",
                caption="Legacy plain video",
                media_url="https://videos.example.com/legacy.mp4",
                scheduled_for=self.now - timedelta(minutes=1),
            )
            session.add(legacy)
            session.flush()
            item_id = legacy.id
        invoked = []
        result = api.execute_due_scheduled_content(
            self.Session,
            now=self.now,
            runtime_resolver=lambda *args, **kwargs: invoked.append("resolver"),
            publisher=lambda *args: invoked.append("publisher"),
            reel_publisher=lambda *args: invoked.append("reel_publisher"),
        )
        item = self._item(item_id)
        self.assertEqual((result.claimed, result.failed), (1, 1))
        self.assertEqual(invoked, [])
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.publish_error_code, "unsupported_content_type")
        self.assertFalse(item.provider_request_may_have_been_sent)


if __name__ == "__main__":
    unittest.main()
