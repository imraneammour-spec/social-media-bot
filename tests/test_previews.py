"""Regression test for standalone previews (pricing preview was broken).

A preview .html file opened on a phone has no server behind it, so any
<link rel="stylesheet" href="/static/..."> or <img src="/static/...">
in it renders unstyled with a broken logo. The generator
(scripts/generate_previews.py) must inline every stylesheet into <style>
and every brand image as a base64 data URI.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_previews", ROOT / "scripts" / "generate_previews.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class PreviewSelfContainedTest(unittest.TestCase):
    def test_stylesheet_links_become_inline_style(self):
        html = (
            '<head><link rel="stylesheet" href="/static/landing.css">'
            '<link rel="stylesheet" href="/static/public_product.css"></head>'
        )
        out = GENERATOR.make_self_contained(html)
        self.assertNotIn('rel="stylesheet"', out)
        self.assertNotIn('href="/static/', out)
        self.assertEqual(out.count("<style>"), 2)
        # Real rules from the app CSS must be inside the file.
        self.assertIn("#FFF8F0", out)
        self.assertIn(".public-product .feature-grid", out)
        self.assertIn("repeat(2,1fr)", out)

    def test_brand_images_become_data_uris(self):
        html = (
            '<img src="/static/brand/postenza-logo-light.png" alt="Postenza">'
            '<img src="/static/brand/postenza-icon.png" alt="Postenza">'
            '<link rel="icon" href="/static/brand/postenza-favicon-32.png">'
        )
        out = GENERATOR.make_self_contained(html)
        self.assertNotIn('src="/static/', out)
        self.assertNotIn('href="/static/', out)
        self.assertGreaterEqual(out.count("data:image/png;base64,"), 3)

    def test_no_static_reference_remains(self):
        html = (
            '<head><link rel="stylesheet" href="/static/landing.css">'
            '<link rel="icon" href="/static/brand/postenza-favicon-32.png">'
            '<meta property="og:image" content="http://localhost/static/brand/postenza-logo-light.png">'
            '</head><body><img src="/static/brand/postenza-logo-dark.png">'
            "</body>"
        )
        out = GENERATOR.make_self_contained(html)
        self.assertNotIn("/static/", out)


if __name__ == "__main__":
    unittest.main()
