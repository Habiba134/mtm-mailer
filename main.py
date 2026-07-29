"""
SBP Mark-to-Market Revaluation Exchange Rate — GitHub Actions Pipeline
=======================================================================
Production-grade Python pipeline designed to run on ephemeral GitHub runners.

Architecture:
- Timezone: Asia/Karachi (PKT) using pytz.
- Single-Run Polling Strategy: loops with a 10-minute interval inside one
  GitHub Actions job run, until the PDF is found or the cutoff time passes.
- Fail-safe Scraping: parses the SBP HTML page with BeautifulSoup4, with a
  fallback to SBP's JSON API (the page is rendered dynamically in JS).
- In-Memory Parsing: downloads the PDF into memory (io.BytesIO) and extracts
  the rate table using pdfplumber to build an HTML email body.
- High-Visibility Alert Email: inline CSS, colored header banner, clean table.
- Cutoff Time: sends a failure alert to the admin if the file isn't found by
  7:00 PM PKT, then exits cleanly.
"""

from __future__ import annotations

import os
import sys
import time
import io
import smtplib
import logging
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.message import EmailMessage
from typing import Any, Optional
from urllib.parse import urljoin

import requests
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import pdfplumber

load_dotenv()

# ─────────────────────────────────────────────────────────────────────
# CONSTANTS & TIME CONFIGURATION
# ─────────────────────────────────────────────────────────────────────

# SBP Target Page URL — Mark-to-Market Revaluation Exchange Rate
SBP_TARGET_URL = "https://www.sbp.org.pk/economic-data/mark-to-market-revaluation-exchange-rate"

# SBP JSON API (fail-safe fallback since the page is client-side rendered)
SBP_API_URL = (
    "https://www.sbp.org.pk/economic-data/"
    "get-economic-data-by-cat-external"
    "?slug=mark-to-market-revaluation-exchange-rate"
)

PKT_ZONE = pytz.timezone("Asia/Karachi")

POLL_INTERVAL_SECS = 600   # 10 minutes
CUTOFF_HOUR = 19           # 7:00 PM PKT
CUTOFF_MINUTE = 0

HTTP_TIMEOUT_SECS = 45
BACKOFF_RETRIES = 3
INITIAL_BACKOFF_SECS = 2.0

# ─────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────

def configure_logging() -> logging.Logger:
    logger = logging.getLogger("sbp_mtm_pipeline")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(handler)
    return logger


log = configure_logging()

# ─────────────────────────────────────────────────────────────────────
# CONFIGURATION LOADER
# ─────────────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    """Loads required configuration from environment variables / GitHub Secrets."""
    required = [
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
        "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"
    ]
    config = {}
    missing = []
    for key in required:
        val = os.getenv(key)
        if not val:
            missing.append(key)
        config[key] = val

    if missing:
        log.critical("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    try:
        config["SMTP_PORT"] = int(config["SMTP_PORT"])
    except (ValueError, TypeError):
        log.critical("SMTP_PORT must be an integer, got: %s", config["SMTP_PORT"])
        sys.exit(1)

    # EMAIL_TO can be a comma-separated list, e.g. "sir@x.com, me@x.com"
    config["EMAIL_TO"] = [
        addr.strip() for addr in config["EMAIL_TO"].split(",") if addr.strip()
    ]

    # ADMIN_EMAIL defaults to first recipient if not explicitly set
    config["ADMIN_EMAIL"] = os.getenv("ADMIN_EMAIL", config["EMAIL_TO"][0])

    return config

# ─────────────────────────────────────────────────────────────────────
# NETWORK LAYER WITH BACKOFF
# ─────────────────────────────────────────────────────────────────────

def fetch_url_with_backoff(url: str) -> requests.Response:
    backoff = INITIAL_BACKOFF_SECS
    for attempt in range(1, BACKOFF_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=HTTP_TIMEOUT_SECS,
                headers={"User-Agent": "SBP-MTM-Automation-GitHubActions/1.0"}
            )
            if resp.status_code == 429 or (500 <= resp.status_code < 600):
                log.warning("HTTP %d from server. Retrying in %ds...", resp.status_code, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning("Network issue (attempt %d/%d): %s. Retrying in %ds...", attempt, BACKOFF_RETRIES, exc, backoff)
            time.sleep(backoff)
            backoff *= 2
    raise ConnectionError(f"Failed to fetch URL: {url} after {BACKOFF_RETRIES} attempts.")

# ─────────────────────────────────────────────────────────────────────
# SCRAPING AND VALIDATION LAYER
# ─────────────────────────────────────────────────────────────────────

def search_pdf_in_html(html_content: str, today_pkt: date) -> Optional[dict[str, str]]:
    """Parses the target page HTML looking for a matching daily PDF link."""
    soup = BeautifulSoup(html_content, "html.parser")

    day = today_pkt.day
    day_padded = f"{day:02d}"
    month_name = today_pkt.strftime("%B")
    month_abbr = today_pkt.strftime("%b")

    date_patterns = [
        f"{day}-{month_name}",
        f"{day_padded}-{month_name}",
        f"{day}-{month_abbr}",
        f"{day_padded}-{month_abbr}"
    ]

    log.info("Searching HTML for PDF links matching date patterns: %s", date_patterns)

    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        anchor_text = a.get_text().strip().lower()
        if href.endswith(".pdf"):
            for pattern in date_patterns:
                pat = pattern.lower()
                if pat in href or pat in anchor_text:
                    absolute_url = urljoin(SBP_TARGET_URL, a["href"])
                    filename = absolute_url.split("/")[-1]
                    log.info("Match found in HTML: text='%s', url=%s", a.get_text().strip(), absolute_url)
                    return {"url": absolute_url, "filename": filename}
    return None


def search_pdf_in_api(today_pkt: date) -> Optional[dict[str, str]]:
    """Fallback parser hitting SBP's JSON API directly."""
    log.info("Falling back to SBP JSON API for data fetching...")
    try:
        resp = fetch_url_with_backoff(SBP_API_URL)
        data = resp.json()

        # SBP API structure: exchange-rates -> mark-to-market-revaluation-exchange-rate -> daily -> {year: [entries]}
        daily_rates = data["exchange-rates"]["mark-to-market-revaluation-exchange-rate"]["daily"]

        today_str = today_pkt.isoformat()

        years = sorted(list(daily_rates.keys()), reverse=True)
        for year in years:
            for entry in daily_rates[year]:
                if entry.get("date") == today_str:
                    att = entry.get("attachment")
                    if att and att.get("url") and att.get("url").lower().endswith(".pdf"):
                        pdf_url = att["url"]
                        filename = att.get("file_name") or pdf_url.split("/")[-1]
                        log.info("Match found in API: date=%s, url=%s", today_str, pdf_url)
                        return {"url": pdf_url, "filename": filename}
    except Exception as exc:
        log.error("Error checking SBP JSON API: %s", exc)
    return None


def validate_pdf_internal_date(pdf_bytes: bytes, today_pkt: date) -> bool:
    """Scans in-memory PDF page 1 text to verify today's date is inside."""
    day = today_pkt.day
    day_padded = f"{day:02d}"
    month_name = today_pkt.strftime("%B")
    month_abbr = today_pkt.strftime("%b")
    year = today_pkt.strftime("%Y")
    year_short = today_pkt.strftime("%y")

    date_patterns = [
        f"{day}-{month_abbr}-{year_short}",
        f"{day_padded}-{month_abbr}-{year_short}",
        f"{day}-{month_name}-{year}",
        f"{day_padded}-{month_name}-{year}",
        f"{day}-{month_abbr}-{year}",
        f"{day_padded}-{month_abbr}-{year}"
    ]

    try:
        pdf_file = io.BytesIO(pdf_bytes)
        with pdfplumber.open(pdf_file) as pdf:
            if not pdf.pages:
                return False
            page1_text = pdf.pages[0].extract_text() or ""
            text_lower = page1_text.lower()
            for pat in date_patterns:
                if pat.lower() in text_lower:
                    log.info("PDF validated. Found internal date pattern: '%s'", pat)
                    return True
            log.warning("Could not find any of the expected date patterns in page 1 text.")
            return False
    except Exception as exc:
        log.error("Failed to read PDF content: %s", exc)
        return False


def generate_html_table(table: list[list[str]]) -> str:
    html = []
    html.append('<table style="width: 100%; border-collapse: collapse; font-family: Arial, sans-serif; margin-top: 15px; border: 1px solid #e2e8f0;">')
    headers = table[0]
    html.append('  <thead>')
    html.append('    <tr style="background-color: #1a365d; border-bottom: 2px solid #e2e8f0;">')
    for idx, header in enumerate(headers):
        align = "left" if idx == 0 else "right"
        val = str(header).strip()
        html.append(f'      <th style="padding: 12px 15px; text-align: {align}; font-weight: bold; color: #ffffff; font-size: 14px; border: 1px solid #edf2f7;">{val}</th>')
    html.append('    </tr>')
    html.append('  </thead>')
    html.append('  <tbody>')
    for row_idx, row in enumerate(table[1:]):
        if not row or len(row) < len(headers):
            continue
        bg_color = "#f7fafc" if row_idx % 2 == 1 else "#ffffff"
        html.append(f'    <tr style="background-color: {bg_color};">')
        for idx, col in enumerate(row[:len(headers)]):
            align = "left" if idx == 0 else "right"
            val = str(col).strip()
            if idx == 0:
                html.append(f'      <td style="padding: 10px 15px; text-align: {align}; color: #2d3748; font-weight: bold; border: 1px solid #edf2f7; font-size: 14px;">{val}</td>')
            else:
                html.append(f'      <td style="padding: 10px 15px; text-align: {align}; color: #4a5568; border: 1px solid #edf2f7; font-size: 14px;">{val}</td>')
        html.append('    </tr>')
    html.append('  </tbody>')
    html.append('</table>')
    return "\n".join(html)


def extract_table_from_pdf(pdf_bytes: bytes) -> str:
    try:
        pdf_file = io.BytesIO(pdf_bytes)
        with pdfplumber.open(pdf_file) as pdf:
            if not pdf.pages:
                return "<p style='color: red;'>Error: PDF contains no pages.</p>"

            first_page = pdf.pages[0]
            tables = first_page.extract_tables()
            if tables:
                return generate_html_table(tables[0])

            text = first_page.extract_text() or ""
            rows = []
            for line in text.split("\n"):
                parts = line.strip().split()
                if len(parts) >= 3 and len(parts[0]) == 3 and parts[0].isalpha() and parts[0].isupper():
                    try:
                        float(parts[1])
                        float(parts[2])
                        rows.append([parts[0], parts[1], parts[2]])
                    except ValueError:
                        continue
            if rows:
                table = [["CURRENCY", "BUYING", "SELLING"]] + rows
                return generate_html_table(table)

            return "<p style='color: orange;'>Warning: Could not extract currency table structure from PDF.</p>"
    except Exception as exc:
        log.error("Failed to extract data table from PDF: %s", exc)
        return f"<p style='color: red;'>Error parsing PDF content: {exc}</p>"


def fetch_and_validate(today_pkt: date) -> Optional[tuple[bytes, str, str]]:
    log.info("Initiating scrape attempt for target date: %s", today_pkt)

    html_content = ""
    try:
        resp = fetch_url_with_backoff(SBP_TARGET_URL)
        html_content = resp.text
    except Exception as exc:
        log.warning("Could not fetch SBP page HTML: %s. Proceeding directly to API.", exc)

    pdf_info = None
    if html_content:
        pdf_info = search_pdf_in_html(html_content, today_pkt)

    if not pdf_info:
        log.info("PDF link not found in HTML. Proceeding to SBP JSON API.")
        pdf_info = search_pdf_in_api(today_pkt)

    if not pdf_info:
        log.info("No matching SBP file found for today's date yet.")
        return None

    try:
        log.info("Downloading SBP PDF into memory: %s", pdf_info["url"])
        resp = fetch_url_with_backoff(pdf_info["url"])
        pdf_bytes = resp.content
    except Exception as exc:
        log.error("Failed to download PDF: %s", exc)
        return None

    if not validate_pdf_internal_date(pdf_bytes, today_pkt):
        log.warning("PDF downloaded but content validation failed. Proceeding anyway.")

    html_table = extract_table_from_pdf(pdf_bytes)
    return pdf_bytes, html_table, pdf_info["filename"]

# ─────────────────────────────────────────────────────────────────────
# EMAIL DISPATCH
# ─────────────────────────────────────────────────────────────────────

def build_alert_html(html_table: str, target_date_str: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>SBP Mark-to-Market Rates Alert</title></head>
<body style="margin: 0; padding: 20px; background-color: #f7fafc; font-family: Arial, sans-serif;">
<div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 4px; overflow: hidden; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);">
<div style="background-color: #1a365d; color: #ffffff; padding: 18px; font-size: 20px; font-weight: bold; text-align: center; letter-spacing: 0.5px;">
NEW SBP MARK-TO-MARKET RATES PUBLISHED
</div>
<div style="padding: 20px;">
<div style="font-size: 14px; color: #718096; margin-bottom: 15px; font-weight: bold; text-transform: uppercase;">
Rates Date: {target_date_str}
</div>
{html_table}
<div style="margin-top: 25px; font-size: 11px; color: #a0aec0; line-height: 1.4; border-top: 1px solid #edf2f7; padding-top: 12px; font-style: italic;">
Disclaimer: This data has been extracted automatically from the State Bank of Pakistan daily sheet. Please refer to the attached official PDF to verify any rates.
</div>
</div>
</div>
</body>
</html>
"""


def email_file(config: dict[str, Any], pdf_bytes: bytes, filename: str, html_table: str, today_pkt: date) -> None:
    pkt_date_str = today_pkt.strftime("%d-%b-%Y")
    subject = f"[SBP] Mark-to-Market Exchange Rates - {pkt_date_str}"

    html_body = build_alert_html(html_table, today_pkt.strftime("%B %d, %Y"))

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = config["EMAIL_FROM"]
    msg["To"] = ", ".join(config["EMAIL_TO"])

    msg_alternative = MIMEMultipart("alternative")
    msg_alternative.attach(MIMEText(html_body, "html"))
    msg.attach(msg_alternative)

    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(attachment)

    log.info("Dispatching SBP PDF and extracted table to recipients: %s", msg["To"])
    send_smtp_email(config, msg)


def email_failure_alert(config: dict[str, Any], today_pkt: date) -> None:
    pkt_date_str = today_pkt.strftime("%d-%b-%Y")
    subject = f"ALERT: SBP Mark-to-Market PDF Missing - {pkt_date_str}"
    body = (
        f"Attention Admin,\n\n"
        f"The SBP Mark-to-Market pipeline failed to locate today's PDF "
        f"before the cutoff time (7:00 PM PKT).\n\n"
        f"Date: {today_pkt.strftime('%A, %d %B %Y')}\n\n"
        f"Please verify manually: {SBP_TARGET_URL}\n"
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["EMAIL_FROM"]
    msg["To"] = config["ADMIN_EMAIL"]
    msg.set_content(body)

    log.warning("Pipeline exceeded cutoff time. Dispatching failure alert to admin: %s", msg["To"])
    send_smtp_email(config, msg)


def send_smtp_email(config: dict[str, Any], msg) -> None:
    host = config["SMTP_HOST"]
    port = config["SMTP_PORT"]

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(msg)

    log.info("Email sent successfully.")

# ─────────────────────────────────────────────────────────────────────
# ORCHESTRATION / MAIN POLLING LOOP
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Initializing SBP Mark-to-Market exchange rate monitoring pipeline.")
    config = load_config()

    now_pkt = datetime.now(PKT_ZONE)
    today_pkt = now_pkt.date()

    if today_pkt.weekday() >= 5:  # Saturday=5, Sunday=6
        log.info("Today is %s (weekend). SBP does not publish. Exiting pipeline.", today_pkt.strftime('%A'))
        sys.exit(0)

    cutoff_time = now_pkt.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, second=0, microsecond=0)
    log.info("Target Date: %s | Poll Cutoff Time: %s PKT", today_pkt, cutoff_time.strftime("%I:%M %p"))

    while True:
        current_time_pkt = datetime.now(PKT_ZONE)

        if current_time_pkt >= cutoff_time:
            log.warning("Current PKT time (%s) exceeded poll cutoff. Shutting down.", current_time_pkt.strftime("%I:%M %p"))
            try:
                email_failure_alert(config, today_pkt)
            except Exception as mail_exc:
                log.critical("Failed to send admin failure alert: %s", mail_exc)
            sys.exit(0)

        log.info("Starting checks at %s PKT...", current_time_pkt.strftime("%I:%M %p"))

        try:
            result = fetch_and_validate(today_pkt)
            if result:
                pdf_bytes, html_table, filename = result
                log.info("Valid daily PDF secured and extracted in-memory. Ready to email.")
                email_file(config, pdf_bytes, filename, html_table, today_pkt)
                log.info("Pipeline executed successfully. Terminating runner session.")
                sys.exit(0)
        except Exception as exc:
            log.error("Pipeline encountered an error during this iteration: %s", exc)

        log.info("Sleeping for %d minutes...", POLL_INTERVAL_SECS // 60)
        time.sleep(POLL_INTERVAL_SECS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Pipeline terminated by user signal.")
        sys.exit(0)
    except Exception as fatal_exc:
        log.critical("Pipeline crashed: %s", fatal_exc, exc_info=True)
        sys.exit(1)
