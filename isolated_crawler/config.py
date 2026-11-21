"""
Central configuration for the agency lead-enrichment pipeline.
Edit values here rather than scattering constants through the code.
"""

from dataclasses import dataclass, field


@dataclass
class Config:
    # --- I/O ---
    input_excel: str = "websites.xlsx"
    website_column: str = "Website"
    output_excel: str = "agency_leads.xlsx"
    hot_leads_excel: str = "hot_leads.xlsx"

    # --- Checkpointing ---
    checkpoint_file: str = "checkpoint.jsonl"
    checkpoint_every: int = 50  # flush to disk every N processed rows

    # --- Concurrency ---
    http_concurrency: int = 100  # concurrent aiohttp requests
    browser_concurrency: int = 30  # concurrent Playwright pages
    domain_concurrency: int = 1  # concurrent requests per single domain

    # --- Crawling ---
    max_pages_per_site: int = 6
    timeout: int = 12  # aiohttp timeout (seconds)
    playwright_timeout: int = 15000  # Playwright timeout (ms)
    retry_count: int = 2
    per_domain_delay: float = 0.5  # seconds between requests to same domain

    # --- Safety timeouts (guard against hangs even if a lower-level
    #     library timeout misbehaves) ---
    site_timeout: int = 90  # hard cap on total time spent per domain
    playwright_hard_timeout: int = 25  # outer wait_for around Playwright fetch
    subpage_fetch_concurrency: int = 4  # concurrent subpage fetches per domain
    link_check_timeout: int = 8  # outer wait_for around broken-link sampling

    # --- Lead scoring thresholds ---
    hot_threshold: int = 80
    warm_threshold: int = 60
    qualified_threshold: int = 40

    # --- Logging ---
    log_file: str = "scraper_debug.log"


CONFIG = Config()

# Business-relevance keyword sets used across modules
GENERIC_EMAIL_LOCALPARTS = {
    "info",
    "contact",
    "sales",
    "hello",
    "support",
    "admin",
    "office",
    "enquiries",
    "inquiries",
    "help",
    "team",
}

PERSONAL_EMAIL_PROVIDERS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "live.com",
    "protonmail.com",
    "gmx.com",
}

IGNORED_EMAIL_DOMAINS = {
    "example.com",
    "domain.com",
    "email.com",
    "sentry.io",
    "wix.com",
    "wixpress.com",
    "godaddy.com",
    "cloudflare.com",
    "schema.org",
    "w3.org",
    "google.com",
    "gstatic.com",
}

INDUSTRY_KEYWORDS = {
    "Dental / Medical": [
        "dentist",
        "dental",
        "clinic",
        "medical",
        "doctor",
        "physician",
        "healthcare",
        "orthodont",
        "surgery",
        "patient",
    ],
    "Real Estate": [
        "real estate",
        "realtor",
        "property",
        "properties",
        "listing",
        "mortgage",
        "broker",
        "housing",
    ],
    "Restaurant": [
        "restaurant",
        "menu",
        "reservation",
        "cuisine",
        "cafe",
        "dine",
        "catering",
        "bistro",
        "eatery",
    ],
    "E-commerce": [
        "shop",
        "cart",
        "checkout",
        "add to cart",
        "store",
        "shipping",
        "product",
        "buy now",
        "sale",
    ],
    "Gym / Fitness": [
        "gym",
        "fitness",
        "workout",
        "personal trainer",
        "yoga",
        "crossfit",
        "membership",
    ],
    "Education": [
        "school",
        "academy",
        "course",
        "curriculum",
        "university",
        "college",
        "tuition",
        "enroll",
        "student",
    ],
    "Legal": [
        "law firm",
        "attorney",
        "lawyer",
        "legal",
        "litigation",
        "counsel",
    ],
    "Automotive": [
        "auto repair",
        "dealership",
        "car dealer",
        "automotive",
        "mechanic",
        "tire",
        "vehicle",
    ],
    "Construction": [
        "construction",
        "contractor",
        "renovation",
        "builder",
        "roofing",
        "remodel",
    ],
    "Beauty / Salon": [
        "salon",
        "spa",
        "beauty",
        "hair",
        "nails",
        "barber",
        "cosmetic",
    ],
    "Hotel / Hospitality": [
        "hotel",
        "resort",
        "booking.com",
        "accommodation",
        "suite",
        "check-in",
        "check-out",
    ],
    "SaaS / Software": [
        "software",
        "saas",
        "api",
        "dashboard",
        "subscription",
        "platform",
        "free trial",
        "pricing plan",
    ],
    "Professional Services": [
        "consulting",
        "accounting",
        "agency",
        "services",
        "consultant",
        "bookkeeping",
    ],
}

BOOKING_RELEVANT_INDUSTRIES = {
    "Dental / Medical",
    "Restaurant",
    "Gym / Fitness",
    "Beauty / Salon",
    "Hotel / Hospitality",
}

WHATSAPP_RELEVANT_INDUSTRIES = {
    "Dental / Medical",
    "Real Estate",
    "Restaurant",
    "Gym / Fitness",
    "Beauty / Salon",
    "Automotive",
    "Construction",
    "Professional Services",
}

HIGH_VALUE_INDUSTRIES = {
    "Dental / Medical",
    "Real Estate",
    "Legal",
    "SaaS / Software",
    "Hotel / Hospitality",
}
