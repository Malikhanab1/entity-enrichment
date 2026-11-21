import unittest
from unittest.mock import patch

from enrichment_mcp.crawlers.website import rank_internal_page
from enrichment_mcp.tools.enrich_business import _website_candidates
from enrichment_mcp.tools.find_decision_makers import find_decision_makers
from enrichment_mcp.tools.agentic_research import (
    assess_business,
    crawl_business_pages,
    compress_research_context,
    normalize_business_entity,
    plan_research_strategy,
)
from enrichment_mcp.tools.contact_store import build_crm_record
from enrichment_mcp.ingestion_layer import IngestionAutomationPipeline


class WebsiteSelectionTests(unittest.TestCase):
    def test_wissen_uses_official_domain_published_by_social_profile(self):
        results = [
            {
                "title": "The Wissen Grammar School | Lahore - Facebook",
                "url": "https://www.facebook.com/wissengrammarschool/",
                "snippet": "The Wissen Grammar School. http://wissengrammarschool.edu.pk",
            },
            {
                "title": "The Wissen Grammar School, Lahore",
                "url": "https://www.findglocal.com/PK/Lahore/wissen",
                "snippet": "Directory listing",
            },
        ]

        candidates = _website_candidates(results, "Wissen Grammar School", "Lahore")

        self.assertEqual(candidates[0]["url"], "http://wissengrammarschool.edu.pk")
        self.assertEqual(candidates[0]["reason"], "URL published in search snippet")
        self.assertNotIn("findglocal.com", [candidate["url"] for candidate in candidates])

    def test_owner_evidence_pages_beat_generic_pages(self):
        self.assertGreater(
            rank_internal_page("https://school.example/about", "Our Leadership Team"),
            rank_internal_page("https://school.example/news", "Latest news"),
        )

    def test_wissen_director_is_returned_from_public_profile_evidence(self):
        async def fake_search(query, num_results=10):
            return {
                "results": [{
                    "title": "Usama Ejaz - Director at Wissen Grammar School | LinkedIn",
                    "url": "https://pk.linkedin.com/in/usama-ejaz-5255b635b",
                    "snippet": "Experience: Wissen Grammar School; Location: Lahore.",
                }],
                "errors": [],
            }

        with patch("enrichment_mcp.tools.find_decision_makers.search_google", fake_search):
            result = __import__("asyncio").run(
                find_decision_makers("Wissen Grammar School", location="Lahore")
            )

        self.assertEqual(result["decision_makers"][0]["name"], "Usama Ejaz")
        self.assertEqual(result["decision_makers"][0]["title"], "Director")
        self.assertEqual(result["decision_makers"][0]["confidence"], "high")

    def test_assessment_reports_a_site_without_crawling_it(self):
        async def fake_search(query, num_results=10):
            return {
                "results": [{
                    "title": "Acme School official website",
                    "url": "https://acmeschool.example",
                    "snippet": "Acme School Lahore official website",
                }],
                "errors": [],
            }

        with patch("enrichment_mcp.tools.agentic_research.search_google", fake_search):
            result = __import__("asyncio").run(assess_business("Acme School", "Lahore"))

        self.assertEqual(result["website_status"], "likely_found")
        self.assertEqual(result["recommended_website"], "https://acmeschool.example")
        self.assertEqual(result["website_candidates"][0]["url"], "https://acmeschool.example")

    def test_agent_crawl_does_not_follow_links_automatically(self):
        async def fake_crawl(*args, **kwargs):
            return kwargs

        with patch("enrichment_mcp.tools.agentic_research.crawl_business_website", fake_crawl):
            result = __import__("asyncio").run(crawl_business_pages("https://acme.example"))

        self.assertFalse(result["follow_relevant_links"])
        self.assertIsNone(result["selected_pages"])
        self.assertEqual(result["max_pages"], 1)
        self.assertGreater(
            rank_internal_page("https://school.example/staff", "Meet our staff"),
            rank_internal_page("https://school.example/services", "Services"),
        )

    def test_agent_crawl_rejects_multiple_selected_pages(self):
        with self.assertRaises(ValueError):
            __import__("asyncio").run(
                crawl_business_pages("https://acme.example", ["https://acme.example/about", "https://acme.example/contact"])
            )

    def test_compress_research_context_reduces_duplicate_evidence_without_losing_entity_facts(self):
        pages = [
            {
                "source_url": "https://acme.example/about",
                "title": "About Acme",
                "text": "Acme Dental Clinic is a dental clinic and dental practice in Lahore. We provide dental care.",
                "status": "ok",
            },
            {
                "source_url": "https://acme.example/contact",
                "title": "Contact Acme",
                "text": "Acme Dental Clinic is a dental clinic and dental practice in Lahore. We provide dental care.",
                "status": "ok",
            },
        ]
        result = __import__("asyncio").run(
            compress_research_context(
                business_name="Acme Dental Clinic",
                website_pages=pages,
                profile={
                    "description": "Acme Dental Clinic is a dental clinic in Lahore.",
                    "industry": "Healthcare",
                    "services": ["dental care", "clinic"],
                },
            )
        )

        self.assertEqual(result["entity"]["name"], "Acme Dental Clinic")
        self.assertEqual(result["entity"]["industry"], "Healthcare")
        self.assertEqual(result["deduped_pages"], 1)
        self.assertIn("dental care", result["reusable_facts"]["services"])
        self.assertLess(len(result["compressed_evidence"]), 300)

    def test_pymapscraper_place_adapter_normalizes_maps_metadata_into_ingestion_row(self):
        pipeline = IngestionAutomationPipeline()

        class _Place:
            def __init__(self):
                self.place_id = "maps-place-001"
                self.name = "Acme Dental Clinic"
                self.address = "Lahore, Pakistan"
                self.address_components = []
                self.phone = "+92 300 0000000"
                self.website = "https://acme.example"
                self.rating = 4.5
                self.review_count = 20
                self.categories = ["Dentist", "Healthcare"]
                self.primary_type = "Dentist"
                self.coordinates = {"lat": 31.5204, "lng": 74.3587}
                self.description = "Acme Dental Clinic in Lahore"
                self.social_links = []

        row = pipeline.normalize_pymapscraper_place(_Place())

        self.assertEqual(row.source, "google_maps")
        self.assertEqual(row.business_name, "Acme Dental Clinic")
        self.assertEqual(row.city, "Lahore, Pakistan")
        self.assertEqual(row.website, "https://acme.example")
        self.assertEqual(row.contact_hint["phone"], "+92 300 0000000")
        self.assertIn("Dentist", row.extra["categories"])

    def test_normalize_business_entity_maps_names_to_canonical_signal_plan(self):
        result = __import__("asyncio").run(
            normalize_business_entity(
                business_name="Acme Dental Clinic Lahore",
                location="Lahore",
                website="https://acme.example",
                profile={
                    "industry": "Healthcare",
                    "services": ["dental care"],
                    "description": "Dental clinic focused on families.",
                },
            )
        )

        self.assertEqual(result["canonical_name"], "Acme Dental Clinic Lahore")
        self.assertIn("clinic", result["tokens"])
        self.assertEqual(result["entity_type"], "business")
        self.assertEqual(result["next_best_action"], "enrich")
        self.assertGreaterEqual(result["confidence"], 0.65)

    def test_plan_research_strategy_suggests_a_multi_tool_route(self):
        result = __import__("asyncio").run(
            plan_research_strategy(
                business_name="Acme Dental Clinic Lahore",
                location="Lahore",
                website="https://acme.example",
                profile={
                    "industry": "Healthcare",
                    "services": ["dental care"],
                    "description": "Dental clinic focused on families.",
                },
            )
        )

        self.assertEqual(result["route"][0], "normalize_business_entity")
        self.assertIn("enrich_business", result["route"])
        self.assertIn("find_decision_makers", result["route"])
        self.assertIn("qualify_lead", result["route"])
        self.assertTrue(result["save_tokens"])

    def test_build_crm_record_materializes_normalized_entity_evidence_and_strategy(self):
        crm = build_crm_record(
            business_name="Acme Dental Clinic Lahore",
            website="https://acme.example",
            normalized_entity={
                "canonical_name": "Acme Dental Clinic Lahore",
                "entity_type": "business",
                "industry": "Healthcare",
                "services": ["dental care"],
                "confidence": 0.83,
            },
            compressed_evidence="Acme Dental Clinic provides dental care in Lahore.",
            opportunity_score=78,
            proposal_path="Website + automation modernization path",
            strategy="Normative research route -> enrich -> find contacts -> qualify",
        )

        self.assertEqual(crm["business_name"], "Acme Dental Clinic Lahore")
        self.assertEqual(crm["opportunity_score"], 78)
        self.assertIn("Acme Dental Clinic", crm["compressed_evidence"])
        self.assertEqual(crm["proposal_path"], "Website + automation modernization path")
        self.assertEqual(crm["strategy"], "Normative research route -> enrich -> find contacts -> qualify")


if __name__ == "__main__":
    unittest.main()
