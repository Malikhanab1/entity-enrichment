"""Human-approved SMTP outreach tools.

No email is sent when a draft is created.  The agent must show the draft to
the user and receive the exact approval phrase before calling the send tool.

HTML template: template.html lives next to this file.
Placeholders injected at send time:
  {{BODY_TEXT}}   — plain paragraphs converted to <p> tags
  {{SENDER_NAME}} — "Rizwan Abdullah"
  {{SENDER_TITLE}}— "Cognivelle Sales Representative"
  [FROM_NAME]     — same as SENDER_NAME (legacy placeholder in template)
  [Business Name] — first line of body or business_name from draft
"""

from __future__ import annotations

import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from .contact_store import create_draft, mark_draft_sent, pending_draft, update_draft_body

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_SENDER_NAME  = "Rizwan Abdullah"
_SENDER_TITLE = "Cognivelle Sales Representative"

_TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def _load_template() -> str:
    """Load template.html; fall back to a bare HTML wrapper if missing."""
    if _TEMPLATE_PATH.exists():
        return _TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        "<!DOCTYPE html><html><body>"
        "{{BODY_HTML}}"
        "<p>{{SENDER_NAME}}<br><small>{{SENDER_TITLE}}</small></p>"
        "</body></html>"
    )


def _body_to_html(plain_body: str) -> str:
    """Convert plain-text paragraphs to HTML <p> blocks."""
    paragraphs = [p.strip() for p in plain_body.strip().split("\n\n") if p.strip()]
    return "\n".join(f"<p style='margin:0 0 16px 0;'>{p}</p>" for p in paragraphs)


def _build_html(business_name: str, plain_body: str) -> str:
    """Render the HTML template with the draft body and sender details injected."""
    template = _load_template()
    body_html = _body_to_html(plain_body)

    # Inject body text — replace the static greeting+opening block in the
    # template with the draft's actual text, keeping the rest of the design.
    # The template has a <!-- BODY_START --> / <!-- BODY_END --> pair; if not
    # present we fall back to inserting after the <h2> heading.
    if "<!-- BODY_START -->" in template and "<!-- BODY_END -->" in template:
        html = re.sub(
            r"<!-- BODY_START -->.*?<!-- BODY_END -->",
            f"<!-- BODY_START -->{body_html}<!-- BODY_END -->",
            template,
            flags=re.DOTALL,
        )
    else:
        # Inject after the closing </h2> tag
        html = re.sub(
            r"(</h2>)",
            rf"\1\n{body_html}",
            template,
            count=1,
        )

    # Replace legacy and new placeholders
    html = html.replace("[Business Name]", business_name)
    html = html.replace("[FROM_NAME]", _SENDER_NAME)
    html = html.replace("{{SENDER_NAME}}", _SENDER_NAME)
    html = html.replace("{{SENDER_TITLE}}", _SENDER_TITLE)
    # Update the title/role line in the signature
    html = re.sub(
        r"Digital Infrastructure Consultant",
        _SENDER_TITLE,
        html,
        count=1,
    )
    return html


def _build_plain(plain_body: str) -> str:
    """Build plain-text version with signature appended."""
    return (
        f"{plain_body.strip()}\n\n"
        f"--\n"
        f"{_SENDER_NAME}\n"
        f"{_SENDER_TITLE}\n"
        f"Cognivelle · cognivelle.com\n"
    )


def create_email_draft(business_name: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
    """Create a persisted email draft. This tool never sends an email."""
    if not _EMAIL_RE.fullmatch(recipient.strip()):
        raise ValueError("recipient must be a valid email address.")
    if not subject.strip() or not body.strip():
        raise ValueError("subject and body must be non-empty.")
    draft_id = secrets.token_urlsafe(12)
    create_draft(draft_id, business_name.strip(), recipient.strip(), subject.strip(), body.strip())
    return {
        "draft_id": draft_id,
        "business_name": business_name.strip(),
        "recipient": recipient.strip(),
        "subject": subject.strip(),
        "body": body.strip(),
        "status": "pending_human_approval",
        "required_user_phrase": f"APPROVE EMAIL {draft_id}",
        "instruction": (
            "Show this draft to the user. Do not send until the user supplies "
            "the exact required phrase. The email will be sent using the HTML "
            "template with the body text injected automatically."
        ),
    }


def send_approved_email(draft_id: str, user_approval_phrase: str) -> dict[str, Any]:
    """Send one pending draft only after the user supplied its exact approval phrase."""
    expected = f"APPROVE EMAIL {draft_id}"
    if user_approval_phrase.strip() != expected:
        raise ValueError(
            "Human approval phrase does not match this draft. Email was not sent."
        )
    draft = pending_draft(draft_id)
    if not draft:
        raise ValueError(
            "Pending draft not found; it may already have been sent or expired."
        )

    host      = os.getenv("SMTP_HOST", "smtp-relay.brevo.com")
    port      = int(os.getenv("SMTP_PORT", os.getenv("PORT", "587")))
    username  = os.getenv("SMTP_USERNAME", os.getenv("BREVO_SMTP_USER", ""))
    password  = os.getenv("SMTP_PASSWORD", os.getenv("BREVO_SMTP_KEY", ""))
    from_email = os.getenv("SMTP_FROM_EMAIL", os.getenv("FROM_EMAIL", ""))

    if not all((username, password, from_email)):
        raise RuntimeError(
            "SMTP is not configured. "
            "Set SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM_EMAIL in mcp/.env."
        )

    business_name = draft.get("business_name") or ""
    plain_body    = draft["body"]

    msg = EmailMessage()
    msg["From"]    = formataddr((_SENDER_NAME, from_email))
    msg["To"]      = draft["recipient"]
    msg["Subject"] = draft["subject"]

    # Plain-text part
    msg.set_content(_build_plain(plain_body))

    # HTML part — template with body injected
    msg.add_alternative(_build_html(business_name, plain_body), subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise RuntimeError(
            f"SMTP send failed; the draft remains pending: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    mark_draft_sent(draft_id)
    return {
        "draft_id":  draft_id,
        "recipient": draft["recipient"],
        "status":    "sent",
    }
