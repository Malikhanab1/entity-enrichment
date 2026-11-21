import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from enrichment_mcp.tools import contact_store, outreach_email
from enrichment_mcp.server import persisted_tool


class ContactStoreAndOutreachTests(unittest.TestCase):
    def test_research_is_saved_and_searchable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            stored = contact_store.save_research(
                "Acme", "https://acme.example",
                [{"name": "Ava", "title": "CEO", "email": "ava@acme.example", "confidence": "high", "source": "search"}],
                {"emails_found": ["hello@acme.example"], "phones_found": ["+1 212 555 0199"]},
            )
            found = contact_store.search_stored_contacts("Acme")

        self.assertEqual(stored["business_id"], 1)
        self.assertTrue(any(row["email"] == "ava@acme.example" for row in found["contacts"]))

    def test_email_requires_phrase_then_sends_once(self):
        environment = {
            "SMTP_USERNAME": "user", "SMTP_PASSWORD": "password", "SMTP_FROM_EMAIL": "me@example.com"
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)), patch.dict(os.environ, environment, clear=False):
            draft = outreach_email.create_email_draft("Acme", "ava@acme.example", "Hello", "A short message")
            with self.assertRaises(ValueError):
                outreach_email.send_approved_email(draft["draft_id"], "yes")
            smtp = MagicMock()
            smtp.__enter__.return_value = smtp
            with patch("enrichment_mcp.tools.outreach_email.smtplib.SMTP", return_value=smtp):
                result = outreach_email.send_approved_email(draft["draft_id"], draft["required_user_phrase"])
            self.assertEqual(result["status"], "sent")
            smtp.send_message.assert_called_once()
            with self.assertRaises(ValueError):
                outreach_email.send_approved_email(draft["draft_id"], draft["required_user_phrase"])

    def test_tool_wrapper_records_success_and_failure(self):
        def successful(value: str):
            return {"value": value}

        def failing():
            raise ValueError("bad input")

        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            self.assertEqual(asyncio.run(persisted_tool(successful)(value="saved")), {"value": "saved"})
            with self.assertRaises(ValueError):
                asyncio.run(persisted_tool(failing)())
            runs = contact_store.list_tool_runs(limit=10)["runs"]

        self.assertEqual([run["status"] for run in runs], ["error", "ok"])
        self.assertEqual(runs[1]["tool_name"], "successful")


if __name__ == "__main__":
    unittest.main()
