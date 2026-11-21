import unittest

from enrichment_mcp.extractors.contacts import extract_contacts_from_pages


class ContactExtractionTests(unittest.TestCase):
    def test_extracts_mailto_tel_and_obfuscated_email(self):
        contacts = extract_contacts_from_pages(
            [{
                "source_url": "https://acme.example/contact",
                "text": "For admissions write hello [at] acme [dot] org or call +1 (212) 555-0199.",
                "contact_links": ["mailto:Principal%40acme.org", "tel:+1-212-555-0199"],
            }],
            "https://acme.example",
        )

        # Emails are normalised to lowercase (RFC 5321 local-part is case-sensitive
        # in theory but universally treated as case-insensitive; we lowercase for dedup).
        self.assertEqual(
            set(contacts["emails_found"]),
            {"hello@acme.org", "principal@acme.org"},
        )
        # At least one phone must be found; accept either tel: form or regex form
        self.assertTrue(len(contacts["phones_found"]) >= 1)
        # Result shape includes new keys
        self.assertIn("social_profiles", contacts)
        self.assertIn("google_maps_url", contacts)


if __name__ == "__main__":
    unittest.main()
