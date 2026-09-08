"""Tenant-safe Content image storage boundary tests."""

import hashlib
import tempfile
import unittest
import uuid
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from werkzeug.datastructures import FileStorage

from database.content_media import (
    MAX_CONTENT_IMAGE_BYTES,
    ContentMediaProviderError,
    ContentMediaUnavailableError,
    ContentMediaValidationError,
    delete_content_image,
    upload_content_image,
)


class _Response:
    def __init__(self, *, ok=True, payload=None, status_code=200, text=""):
        self.ok = ok
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


def _jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color=(85, 40, 180)).save(output, format="JPEG")
    return output.getvalue()


def _upload(data: bytes, *, filename="post.jpg", content_type="image/jpeg") -> FileStorage:
    return FileStorage(
        stream=BytesIO(data),
        filename=filename,
        content_type=content_type,
    )


class ContentMediaStorageTest(unittest.TestCase):
    def setUp(self):
        self.configuration = {
            "CLOUDINARY_CLOUD_NAME": "postenza-test",
            "CLOUDINARY_API_KEY": "public-api-key",
            "CLOUDINARY_API_SECRET": "private-api-secret",
        }
        self.account_id = uuid.UUID("11111111-2222-3333-4444-555555555555")

    def test_upload_accepts_decoded_jpeg_and_returns_only_safe_asset_metadata(self):
        provider_asset_id = "postenza/content/11111111222233334444555555555555/0123456789abcdef0123456789abcdef"
        response = _Response(payload={
            "public_id": provider_asset_id,
            "secure_url": "https://res.cloudinary.com/postenza-test/image/upload/post.jpg",
            "api_key": "must-not-be-returned",
        })
        with patch.dict("os.environ", self.configuration, clear=False), patch(
            "database.content_media.uuid.uuid4",
            return_value=type("Uuid", (), {"hex": "0123456789abcdef0123456789abcdef"})(),
        ), patch("database.content_media.requests.post", return_value=response) as provider_post:
            asset = upload_content_image(_upload(_jpeg_bytes()), instagram_account_id=self.account_id)

        self.assertEqual(asset.url, "https://res.cloudinary.com/postenza-test/image/upload/post.jpg")
        self.assertEqual(asset.asset_id, provider_asset_id)
        self.assertEqual(set(asset.__dict__), {"url", "asset_id"})
        request = provider_post.call_args
        self.assertEqual(request.args[0], "https://api.cloudinary.com/v1_1/postenza-test/image/upload")
        self.assertEqual(request.kwargs["files"]["file"][2], "image/jpeg")
        self.assertEqual(request.kwargs["data"]["public_id"], provider_asset_id)
        expected_signature = hashlib.sha1(
            (
                f"public_id={provider_asset_id}&timestamp={request.kwargs['data']['timestamp']}"
                "private-api-secret"
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(request.kwargs["data"]["signature"], expected_signature)
        self.assertNotIn("private-api-secret", str(asset))

    def test_upload_rejects_oversize_before_calling_provider(self):
        with patch.dict("os.environ", self.configuration, clear=False), patch(
            "database.content_media.requests.post"
        ) as provider_post:
            with self.assertRaises(ContentMediaValidationError) as raised:
                upload_content_image(_upload(b"x" * (MAX_CONTENT_IMAGE_BYTES + 1)), instagram_account_id=self.account_id)
        self.assertEqual(raised.exception.code, "image_too_large")
        provider_post.assert_not_called()

    def test_upload_rejects_declared_non_jpeg_and_malformed_jpeg(self):
        for upload in (
            _upload(_jpeg_bytes(), filename="post.png", content_type="image/png"),
            _upload(b"not-an-image", filename="post.jpg", content_type="image/jpeg"),
        ):
            with self.subTest(filename=upload.filename), patch.dict(
                "os.environ", self.configuration, clear=False
            ), patch("database.content_media.requests.post") as provider_post:
                with self.assertRaises(ContentMediaValidationError) as raised:
                    upload_content_image(upload, instagram_account_id=self.account_id)
                self.assertEqual(raised.exception.code, "invalid_jpeg")
                provider_post.assert_not_called()

    def test_upload_fails_safely_when_configuration_is_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ContentMediaUnavailableError) as raised:
                upload_content_image(_upload(_jpeg_bytes()), instagram_account_id=self.account_id)
        self.assertEqual(raised.exception.code, "upload_unavailable")
        self.assertNotIn("CLOUDINARY", str(raised.exception))

    def test_provider_failure_does_not_expose_response_or_credentials(self):
        response = _Response(
            ok=False,
            status_code=401,
            text="raw provider response private-api-secret bearer-token",
        )
        with patch.dict("os.environ", self.configuration, clear=False), patch(
            "database.content_media.requests.post", return_value=response
        ):
            with self.assertRaises(ContentMediaProviderError) as raised:
                upload_content_image(_upload(_jpeg_bytes()), instagram_account_id=self.account_id)
        rendered = str(raised.exception)
        self.assertEqual(raised.exception.code, "upload_failed")
        self.assertNotIn("private-api-secret", rendered)
        self.assertNotIn("bearer-token", rendered)
        self.assertNotIn("raw provider", rendered)

    def test_successful_upload_with_unusable_metadata_attempts_orphan_cleanup(self):
        asset_id = "postenza/content/11111111222233334444555555555555/0123456789abcdef0123456789abcdef"
        responses = [
            _Response(payload={"public_id": asset_id, "secure_url": "http://unsafe.example/post.jpg"}),
            _Response(payload={"result": "ok"}),
        ]
        with patch.dict("os.environ", self.configuration, clear=False), patch(
            "database.content_media.uuid.uuid4",
            return_value=type("Uuid", (), {"hex": "0123456789abcdef0123456789abcdef"})(),
        ), patch("database.content_media.requests.post", side_effect=responses) as provider_post:
            with self.assertRaises(ContentMediaProviderError):
                upload_content_image(_upload(_jpeg_bytes()), instagram_account_id=self.account_id)
        self.assertEqual(provider_post.call_count, 2)
        self.assertTrue(provider_post.call_args.args[0].endswith("/image/destroy"))

    def test_delete_is_prefix_guarded_and_best_effort(self):
        valid_asset_id = "postenza/content/11111111222233334444555555555555/0123456789abcdef0123456789abcdef"
        with patch.dict("os.environ", self.configuration, clear=False), patch(
            "database.content_media.requests.post",
            return_value=_Response(payload={"result": "ok"}),
        ) as provider_post:
            self.assertTrue(delete_content_image(valid_asset_id))
            self.assertFalse(delete_content_image("another-app/customer-asset"))
        self.assertEqual(provider_post.call_count, 1)
        self.assertEqual(
            provider_post.call_args.args[0],
            "https://api.cloudinary.com/v1_1/postenza-test/image/destroy",
        )
        self.assertEqual(provider_post.call_args.kwargs["data"]["public_id"], valid_asset_id)

    def test_upload_does_not_create_application_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", self.configuration, clear=False
        ), patch(
            "database.content_media.uuid.uuid4",
            return_value=type("Uuid", (), {"hex": "0123456789abcdef0123456789abcdef"})(),
        ), patch(
            "database.content_media.requests.post",
            return_value=_Response(payload={
                "public_id": "postenza/content/11111111222233334444555555555555/0123456789abcdef0123456789abcdef",
                "secure_url": "https://res.cloudinary.com/postenza-test/image/upload/post.jpg",
            }),
        ):
            before = set(Path(directory).iterdir())
            upload_content_image(_upload(_jpeg_bytes()), instagram_account_id=self.account_id)
            self.assertEqual(set(Path(directory).iterdir()), before)


if __name__ == "__main__":
    unittest.main()
