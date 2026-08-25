import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_draft_publisher():
    module_path = Path(__file__).resolve().parents[1] / "webhook.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "publish_instagram_draft"
    )
    namespace = {
        "INSTAGRAM_ACCESS_TOKEN": "test-token",
        "INSTAGRAM_USER_ID": "test-account",
        "cloudinary_is_configured": lambda: True,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(module_path), "exec"), namespace)
    return namespace["publish_instagram_draft"]


def load_legacy_approval_route():
    module_path = Path(__file__).resolve().parents[1] / "webhook.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "approve_dashboard_draft"
    )
    function.decorator_list = []
    request = SimpleNamespace(
        files=SimpleNamespace(getlist=lambda _name: []),
        form=SimpleNamespace(get=lambda _name, _default="": ""),
    )
    namespace = {"request": request}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(module_path), "exec"), namespace)
    return namespace["approve_dashboard_draft"]


class PostPublishingDisabledTest(unittest.TestCase):
    def test_draft_publisher_refuses_internal_calls(self):
        publish_instagram_draft = load_draft_publisher()

        media_id, error = publish_instagram_draft({}, "")

        self.assertIsNone(media_id)
        self.assertIn("Post auto-publishing is disabled", error)

    def test_legacy_draft_approval_route_is_disabled(self):
        approve_dashboard_draft = load_legacy_approval_route()

        payload, status = approve_dashboard_draft()

        self.assertEqual(status, 410)
        self.assertIn("Post auto-publishing is disabled", payload["error"])


if __name__ == "__main__":
    unittest.main()
