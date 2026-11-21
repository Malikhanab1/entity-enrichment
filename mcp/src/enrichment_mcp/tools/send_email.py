import os
import csv
import time
import random
import argparse
import smtplib
import logging
from pathlib import Path
from email.message import EmailMessage
from email.utils import formataddr

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

load_dotenv()

FROM_NAME = os.environ["FROM_NAME"]

SMTP_SERVER = "smtp-relay.brevo.com"
SMTP_PORT = os.environ["PORT"]

SMTP_USER = os.environ["BREVO_SMTP_USER"]
SMTP_PASSWORD = os.environ["BREVO_SMTP_KEY"]

FROM_EMAIL = os.environ["FROM_EMAIL"]

SUBJECT = "Your digital presence should match your business"

# Random delay between emails.
# Keep this reasonably conservative for cold outreach.
MIN_DELAY = 300
MAX_DELAY = 600

BASE_DIR = Path(__file__).resolve().parent

TEMPLATE_FILE = BASE_DIR / "template.html"
SENT_LOG = BASE_DIR / "sent.log"
ERROR_LOG = BASE_DIR / "errors.log"

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Template
# ─────────────────────────────────────────────────────────────


def load_template():
    """Load the HTML email template from the same directory."""

    if not TEMPLATE_FILE.exists():
        raise FileNotFoundError(f"Could not find template.html at: {TEMPLATE_FILE}")

    return TEMPLATE_FILE.read_text(encoding="utf-8")


def populate_template(template, business_name):
    """Replace placeholders in the HTML template."""

    return template.replace("[Business Name]", business_name).replace(
        "[FROM_NAME]", FROM_NAME
    )


# ─────────────────────────────────────────────────────────────
# Plain-text fallback
# ─────────────────────────────────────────────────────────────


def create_plain_text(business_name):
    """
    Plain-text fallback for clients that cannot render HTML.
    """

    return f"""Hi {business_name} Team,

You deliver exceptional results, but taking a look at your current
digital setup, there appears to be a disconnect between the quality
of your business and your online client experience.

At Cognivelle, we can help modernize your digital infrastructure through:

A. Website redesign
A conversion-focused website that better represents your business.

B. Online booking
A streamlined booking system that lets clients schedule appointments
directly online.

C. AI chatbot
A chatbot that can handle common business questions and help clients
find the information they need 24/7.

If you'd like to explore this, you can schedule a short call here:

https://calendly.com/hello-cognivelle

Or simply reply to this email.

Best,

Rizwan Abdullah
Digital Infrastructure Consultant
cognivelle.com
"""


# ─────────────────────────────────────────────────────────────
# Sent tracking
# ─────────────────────────────────────────────────────────────


def load_sent_emails():
    """Return a set of email addresses that have already been sent."""

    if not SENT_LOG.exists():
        return set()

    sent = set()

    with SENT_LOG.open("r", encoding="utf-8") as file:
        for line in file:
            email = line.strip().lower()

            if email:
                sent.add(email)

    return sent


def mark_as_sent(email):
    """Append an email to the sent log."""

    with SENT_LOG.open("a", encoding="utf-8") as file:
        file.write(email.lower() + "\n")


def log_error(email, error):
    """Write failed deliveries to errors.log."""

    with ERROR_LOG.open("a", encoding="utf-8") as file:
        file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {email} | {error}\n")


# ─────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────


def load_leads(csv_file):
    """
    Load leads from:

        name,email
        ABC Dental,hello@abcdental.com
        XYZ Clinic,contact@xyzclinic.com
    """

    leads = []

    with open(csv_file, "r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if not reader.fieldnames:
            raise ValueError("CSV file has no header.")

        # Normalize headers
        headers = {header.strip().lower(): header for header in reader.fieldnames}

        if "name" not in headers or "email" not in headers:
            raise ValueError("CSV must contain 'name' and 'email' columns.")

        for row_number, row in enumerate(reader, start=2):
            name = row[headers["name"]].strip()
            email = row[headers["email"]].strip()

            if not email:
                logger.warning(f"Skipping row {row_number}: missing email.")
                continue

            if not name:
                name = "Your"

            leads.append(
                {
                    "name": name,
                    "email": email,
                }
            )

    return leads


# ─────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────


def create_message(recipient, business_name, template):
    """Create a multipart HTML + plain-text email."""

    html_body = populate_template(template, business_name)

    plain_body = create_plain_text(business_name)

    msg = EmailMessage()

    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))

    msg["To"] = recipient
    msg["Subject"] = SUBJECT

    # Plain-text fallback
    msg.set_content(plain_body)

    # HTML version
    msg.add_alternative(html_body, subtype="html")

    return msg


def send_email(recipient, business_name, template):
    """Send one email through Brevo SMTP."""

    message = create_message(
        recipient,
        business_name,
        template,
    )

    with smtplib.SMTP(
        SMTP_SERVER,
        SMTP_PORT,
        timeout=30,
    ) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()

        server.login(
            SMTP_USER,
            SMTP_PASSWORD,
        )

        server.send_message(message)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────


def main():

    parser = argparse.ArgumentParser(
        description="Send Cognivelle outreach emails from a CSV."
    )

    parser.add_argument(
        "csv",
        help="CSV file containing name,email",
    )

    parser.add_argument(
        "--num",
        type=int,
        default=None,
        help=("Maximum number of new emails to send (excluding already-sent emails)."),
    )

    parser.add_argument(
        "--min-delay",
        type=int,
        default=MIN_DELAY,
        help=f"Minimum delay between emails (default: {MIN_DELAY}s)",
    )

    parser.add_argument(
        "--max-delay",
        type=int,
        default=MAX_DELAY,
        help=f"Maximum delay between emails (default: {MAX_DELAY}s)",
    )

    parser.add_argument(
        "--test",
        action="store_true",
        help="Only validate the CSV/template without sending emails.",
    )

    args = parser.parse_args()

    if args.min_delay > args.max_delay:
        raise ValueError("--min-delay cannot be greater than --max-delay")

    if args.num is not None and args.num <= 0:
        raise ValueError("--num must be greater than 0")

    # Load template once.
    template = load_template()

    # Load CSV.
    leads = load_leads(args.csv)

    # Load previous successful sends.
    sent_emails = load_sent_emails()

    logger.info(f"Loaded {len(leads)} leads.")
    logger.info(f"{len(sent_emails)} emails already recorded as sent.")

    if args.num is not None:
        logger.info(f"Email limit: {args.num} new emails.")

    # ─────────────────────────────────────────────────────────
    # Test mode
    # ─────────────────────────────────────────────────────────

    if args.test:
        logger.info("TEST MODE — no emails will be sent.")

        for lead in leads:
            status = "ALREADY SENT" if lead["email"].lower() in sent_emails else "READY"

            print(f"[{status}] {lead['name']} <{lead['email']}>")

        return

    # ─────────────────────────────────────────────────────────
    # Send emails
    # ─────────────────────────────────────────────────────────

    successful = 0
    skipped = 0
    failed = 0

    for index, lead in enumerate(leads, start=1):
        # Stop once the requested number of NEW emails
        # have been successfully sent.
        if args.num is not None and successful >= args.num:
            logger.info(f"Reached --num limit of {args.num} emails.")
            break

        name = lead["name"]
        email = lead["email"]

        normalized_email = email.lower()

        # Don't send duplicates.
        # Already-sent emails do NOT count toward --num.
        if normalized_email in sent_emails:
            logger.info(f"[{index}/{len(leads)}] Skipping {email} — already sent.")

            skipped += 1
            continue

        logger.info(f"[{index}/{len(leads)}] Sending to {name} <{email}>...")

        try:
            send_email(
                recipient=email,
                business_name=name,
                template=template,
            )

            # Only mark as sent after successful delivery.
            mark_as_sent(email)
            sent_emails.add(normalized_email)

            successful += 1

            logger.info(
                f"Successfully sent to {email} "
                f"({successful}"
                + (f"/{args.num}" if args.num is not None else "")
                + ")"
            )

        except Exception as exc:
            failed += 1

            log_error(
                email,
                repr(exc),
            )

            logger.exception(f"Failed to send to {email}")

        # If this was the final requested email,
        # don't wait unnecessarily.
        if args.num is not None and successful >= args.num:
            break

        # Don't delay after the final lead.
        if index < len(leads):
            delay = random.uniform(
                args.min_delay,
                args.max_delay,
            )

            logger.info(f"Waiting {delay:.1f} seconds...")

            time.sleep(delay)

    # ─────────────────────────────────────────────────────────
    # Summary
    # ─────────────────────────────────────────────────────────

    print()
    print("────────────────────────────────────")
    print("Cognivelle outreach complete")
    print("────────────────────────────────────")
    print(f"Successful : {successful}")
    print(f"Skipped    : {skipped}")
    print(f"Failed     : {failed}")

    if args.num is not None:
        print(f"Requested  : {args.num}")

    print("────────────────────────────────────")


if __name__ == "__main__":
    main()
