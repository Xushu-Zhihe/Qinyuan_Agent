from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qsubagent.runtime import ThreadRepository
from qsubagent.security import redact_data, redact_text


class RedactionTests(unittest.TestCase):
    def test_generic_secrets_and_credentialed_urls_are_redacted(self) -> None:
        value = {
            "sandbox_token": "bridge-secret",
            "password": "pw-secret",
            "nested": "https://user:pass@example.test/path",
        }

        redacted = redact_data(value)

        self.assertEqual(redacted["sandbox_token"], "[REDACTED]")
        self.assertEqual(redacted["password"], "[REDACTED]")
        self.assertNotIn("user:pass", redacted["nested"])

    def test_sensitive_headers_and_fields_are_redacted(self) -> None:
        data = redact_data(
            {
                "Authorization": "Bearer abc.def",
                "api_key": "secret",
                "message": "Cookie=session-id; Bearer token-value",
            }
        )

        self.assertEqual(data["Authorization"], "[REDACTED]")
        self.assertEqual(data["api_key"], "[REDACTED]")
        self.assertNotIn("session-id", data["message"])
        self.assertNotIn("token-value", data["message"])

    def test_event_database_and_jsonl_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ThreadRepository(Path(directory), "thread")
            repository.append_event(
                {
                    "type": "error",
                    "Authorization": "Bearer top-secret",
                    "error": "api_key=another-secret",
                }
            )

            event = list(repository.iter_events())[-1]
            audit = json.loads(
                repository.events_path.read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(event["Authorization"], "[REDACTED]")
            self.assertNotIn("another-secret", event["error"])
            self.assertNotIn("top-secret", json.dumps(audit))

    def test_plain_text_redaction_keeps_non_secret_text(self) -> None:
        value = redact_text("request failed: Authorization: Bearer secret-token")
        self.assertIn("request failed", value)
        self.assertNotIn("secret-token", value)
