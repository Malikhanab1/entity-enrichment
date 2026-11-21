import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enrichment_mcp.tools import contact_store


class CRMAndIngestionTests(unittest.TestCase):
    def test_crm_business_and_contact_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            biz_id = contact_store.upsert_business(
                name="Nexus Tech",
                website="https://nexus.example",
                city="London",
                phone="+44 20 7946 0991",
                email="info@nexus.example",
                notes="Target web modernization lead",
                opportunity_score=88,
            )
            self.assertIsInstance(biz_id, int)

            contact_id = contact_store.add_business_contact(
                business_name="Nexus Tech",
                name="Sarah Connor",
                title="CTO",
                email="sarah@nexus.example",
                phone="+44 7700 900077",
                linkedin_url="https://linkedin.com/in/sarah-connor",
            )
            self.assertTrue(contact_id >= 0)

            found = contact_store.search_stored_contacts("Nexus")
            self.assertTrue(any(c["name"] == "Sarah Connor" for c in found["contacts"]))

            deleted = contact_store.delete_business("Nexus Tech")
            self.assertTrue(deleted)
            found_after = contact_store.search_stored_contacts("Nexus")
            self.assertEqual(len(found_after["contacts"]), 0)

    def test_whatsapp_draft_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            contact_store.upsert_business(name="Apex Systems")
            draft = contact_store.create_whatsapp_draft(
                business_name="Apex Systems",
                recipient_phone="+92 300 1234567",
                message="Hi! Reaching out regarding your software modernization.",
            )
            self.assertEqual(draft["status"], "pending")

            drafts = contact_store.list_whatsapp_drafts("pending")
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0]["recipient_phone"], "+923001234567")

            contact_store.update_whatsapp_draft(draft["draft_id"], "Updated pitch message")
            drafts = contact_store.list_whatsapp_drafts("pending")
            self.assertEqual(drafts[0]["message"], "Updated pitch message")

            contact_store.mark_whatsapp_draft_sent(draft["draft_id"])
            self.assertEqual(len(contact_store.list_whatsapp_drafts("pending")), 0)
            self.assertEqual(len(contact_store.list_whatsapp_drafts("sent")), 1)

    def test_single_database_places_sync_and_unpopulated_leads(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            with contact_store._connection() as conn:
                conn.execute(
                    """INSERT INTO places (place_id, name, address, website, phone, rating, review_count)
                       VALUES ('p1', 'Baltimore Dental Care', '123 Main St, Baltimore, MD', 'https://bmore-dental.example', '410-555-0199', 4.8, 42)"""
                )

            synced = contact_store.sync_places_to_unpopulated_leads()
            self.assertEqual(synced, 1)

            with contact_store._connection() as conn:
                row = conn.execute("SELECT * FROM businesses WHERE name='Baltimore Dental Care'").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["outreach_status"], "unpopulated")
                self.assertEqual(row["lead_tier"], "UNPOPULATED")
                self.assertEqual(row["city"], "Baltimore")

    def test_team_member_assignment_and_whatsapp_dispatch(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            contact_store.upsert_business(
                name="Karachi Logistics",
                phone="+92 300 9876543",
                city="Karachi",
            )
            # Default seeded team members exist
            tms = contact_store.get_team_members(active_only=True)
            self.assertTrue(len(tms) >= 4)

            # Assign lead - phone channel routes to caller
            res = contact_store.assign_business_lead("Karachi Logistics", channel="call", notify=False)
            self.assertIn("assigned_name", res)
            self.assertEqual(res["assigned_role"], "caller")

            with contact_store._connection() as conn:
                biz = conn.execute("SELECT assigned_member_name FROM businesses WHERE name='Karachi Logistics'").fetchone()
                self.assertEqual(biz["assigned_member_name"], res["assigned_name"])

    def test_comprehensive_scoring_pipeline(self):
        from enrichment_mcp.tools.lead_scoring import calculate_lead_score, score_all_unscored_leads

        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            # Test dental clinic with no booking and no WhatsApp
            biz = {
                "name": "Downtown Dental Clinic",
                "website": "http://insecure-dentist.example",
                "notes": "Dental practice in Baltimore",
            }
            res = calculate_lead_score(biz)
            self.assertTrue(res["opportunity_score"] >= 40)
            self.assertIn(res["lead_tier"], ["HOT", "WARM"])
            self.assertTrue(len(res["score_breakdown"]) > 0)
            self.assertIn("Appointment Automation", res["recommended_service"])

            # Test batch scoring
            contact_store.upsert_business(name="Test Practice", notes="dentist clinic in Baltimore")
            batch_res = score_all_unscored_leads(limit=10)
            self.assertTrue(batch_res["scored_count"] >= 1)

    def test_populate_lead_engine(self):
        import asyncio
        from enrichment_mcp.tools.populate_lead import populate_lead

        with tempfile.TemporaryDirectory() as directory, patch.object(contact_store, "WORKSPACE", Path(directory)):
            contact_store.upsert_business(
                name="Apex Coaching Centre",
                city="Baltimore",
                outreach_status="unpopulated",
            )
            # Mock external network enrichment calls to keep test fast and isolated
            with patch("enrichment_mcp.tools.populate_lead.enrich_business", return_value={"website": "https://apex.example", "description": "Coaching institute"}), \
                 patch("enrichment_mcp.tools.populate_lead.crawl_website_for_contacts", return_value={"emails_found": ["info@apex.example"], "phones_found": ["+1 410-555-1234"], "social_profiles": {}}), \
                 patch("enrichment_mcp.tools.populate_lead.find_decision_makers", return_value={"decision_makers": [{"name": "Dr. Sarah", "title": "Director"}]}):

                result = asyncio.run(populate_lead("Apex Coaching Centre"))
                self.assertEqual(result["status"], "populated")
                self.assertEqual(result["website"], "https://apex.example")
                self.assertEqual(result["outreach_status"], "not_contacted")
                self.assertTrue(result["opportunity_score"] > 0)

                with contact_store._connection() as conn:
                    biz = conn.execute("SELECT * FROM businesses WHERE name='Apex Coaching Centre'").fetchone()
                    self.assertEqual(biz["outreach_status"], "not_contacted")
                    self.assertIsNotNone(biz["assigned_member_name"])


if __name__ == "__main__":
    unittest.main()

