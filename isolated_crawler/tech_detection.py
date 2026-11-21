"""Lightweight, string/marker-based technology & CMS detection.

Deliberately cheap: everything here works off the already-downloaded HTML
(no extra requests), since we only get one fetch per page in the pipeline.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

CMS_MARKERS = {
    "WordPress": ["wp-content", "wp-includes", "/wp-json/"],
    "Shopify": ["cdn.shopify.com", "Shopify.theme", "myshopify.com"],
    "Wix": ["wix.com", "wixstatic.com", "_wixCssHeadOverride"],
    "Squarespace": ["squarespace.com", "static1.squarespace.com"],
    "Webflow": ["webflow.com", "data-wf-site", "data-wf-page"],
}

FRAMEWORK_MARKERS = {
    "React": ["__next", "react-dom", "data-reactroot", "_reactListening"],
    "Next.js": ["__NEXT_DATA__", "/_next/static/"],
    "Vue": ["__vue__", "data-v-", "vue.js", "vue.min.js"],
    "Angular": ["ng-version", "ng-app", "angular.min.js"],
    "Bootstrap": ["bootstrap.min.css", "bootstrap.bundle"],
    "Tailwind": ["tailwindcss", "tailwind.min.css"],
}


def detect_technology(html_text: str, soup: BeautifulSoup) -> tuple[str, str]:
    """
    Returns (technology_csv, cms). `technology_csv` lists every detected
    technology; `cms` is set only when a CMS is confidently identified
    (i.e., exactly one CMS marker set matched).
    """
    html_lower = html_text.lower()
    detected = []
    detected_cms = []

    for name, markers in CMS_MARKERS.items():
        if any(marker.lower() in html_lower for marker in markers):
            detected.append(name)
            detected_cms.append(name)

    for name, markers in FRAMEWORK_MARKERS.items():
        if any(marker.lower() in html_lower for marker in markers):
            detected.append(name)

    # Generator meta tag is a strong secondary CMS signal
    generator = soup.find("meta", attrs={"name": "generator"})
    if generator and generator.get("content"):
        gen_content = generator["content"]
        if gen_content and gen_content not in detected:
            detected.append(gen_content)
            detected_cms.append(gen_content)

    cms = detected_cms[0] if len(set(detected_cms)) == 1 else ""

    return ", ".join(dict.fromkeys(detected)), cms
