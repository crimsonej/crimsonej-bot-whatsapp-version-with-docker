import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DIR = os.path.join(ROOT, "crimson-bot")
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)


class RuntimeGuardTests(unittest.TestCase):
    def test_public_url_rejects_local_hosts(self):
        from services.web_reader import _public_url

        self.assertFalse(_public_url("http://127.0.0.1/")[0])
        self.assertFalse(_public_url("http://localhost/")[0])

    def test_tool_payload_includes_tools(self):
        import core.llm as llm

        class Completions:
            def create(self, **payload):
                self.payload = payload
                return type("Response", (), {"choices": []})()

        completions = Completions()
        fake_client = type("Client", (), {
            "chat": type("Chat", (), {"completions": completions})()
        })()
        with patch.object(llm, "nvidia_client", fake_client):
            llm._call_provider("nvidia", [{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        self.assertEqual(completions.payload["tools"], [{"type": "function"}])
        self.assertEqual(completions.payload["tool_choice"], "auto")

    def test_flask_control_route_requires_token(self):
        os.environ["CRIMSON_API_TOKEN"] = "test-runtime-token"
        from bot import app

        client = app.test_client()
        self.assertEqual(client.post("/sent_ids", json={"message_id": "x"}).status_code, 401)
        self.assertEqual(
            client.post(
                "/sent_ids",
                json={"message_id": "x"},
                headers={"Authorization": "Bearer test-runtime-token"},
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
