#!/usr/bin/env python3
"""
MineRun – Automated Follow-up Mailer
=====================================
Reads contacts from a Google Sheet (Email + Producto columns),
sends a personalised follow-up email to every new contact via
Hostalia SMTP, and stamps the row with the send timestamp so the
same contact is never emailed twice.

Usage
-----
  python mailer.py [--dry-run]

  --dry-run  Print the emails that would be sent without actually
             sending them or modifying the sheet.
"""

import argparse
import logging
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# ── Load .env ────────────────────────────────────────────────────────────────
load_dotenv()

# ── Config from environment ──────────────────────────────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "correo.hostalia.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_NAME = os.getenv("FROM_NAME", "MineRun")

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Clientes")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

COL_EMAIL = os.getenv("COL_EMAIL", "Email")
COL_PRODUCT = os.getenv("COL_PRODUCT", "Producto")
COL_SENT = "Enviado"

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("mailer.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Google Sheets scopes ─────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def get_worksheet() -> gspread.Worksheet:
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


def ensure_sent_column(sheet: gspread.Worksheet, headers: list) -> int:
    """Return the 1-based column index for COL_SENT, creating it if absent."""
    if COL_SENT in headers:
        return headers.index(COL_SENT) + 1
    new_col = len(headers) + 1
    sheet.update_cell(1, new_col, COL_SENT)
    log.info(f"Created '{COL_SENT}' column at position {new_col}.")
    return new_col


def build_email(to_email: str, product: str) -> MIMEMultipart:
    subject = f"¿Sigues interesado/a en {product}?"

    body_text = f"""\
Hola,

Hace un tiempo mostraste interés en {product} y nos gustaría saber \
si podemos ayudarte a dar el siguiente paso.

En MineRun llevamos años ayudando a nuestros clientes a conseguir \
sus objetivos y queremos hacer lo mismo por ti.

¿Tienes unos minutos para charlar? Puedes responder a este correo \
o llamarnos directamente y lo resolvemos juntos.

Un saludo,
{FROM_NAME}

──────────────────────────────────────────────
Si no deseas recibir más correos, responde con
el asunto "BAJA" y te eliminamos de inmediato.
"""

    body_html = f"""\
<html><body style="font-family:Arial,sans-serif;font-size:15px;color:#222;">
  <p>Hola,</p>
  <p>
    Hace un tiempo mostraste interés en <strong>{product}</strong> y nos
    gustaría saber si podemos ayudarte a dar el siguiente paso.
  </p>
  <p>
    En MineRun llevamos años ayudando a nuestros clientes a conseguir
    sus objetivos y queremos hacer lo mismo por ti.
  </p>
  <p>
    ¿Tienes unos minutos para charlar? Puedes responder a este correo
    o llamarnos directamente y lo resolvemos juntos.
  </p>
  <p>Un saludo,<br/><strong>{FROM_NAME}</strong></p>
  <hr/>
  <p style="font-size:11px;color:#888;">
    Si no deseas recibir más correos, responde con el asunto
    <em>BAJA</em> y te eliminamos de inmediato.
  </p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    return msg


def open_smtp_connection():
    """Return an authenticated SMTP connection (SSL or STARTTLS)."""
    if SMTP_PORT == 587:
        ctx = ssl.create_default_context()
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo()
        conn.starttls(context=ctx)
        conn.ehlo()
    else:
        conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)

    conn.login(SMTP_USER, SMTP_PASS)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main(dry_run: bool = False) -> None:
    # Validate required env vars
    missing = [v for v in ("SMTP_USER", "SMTP_PASS", "GOOGLE_SHEET_ID") if not os.getenv(v)]
    if missing:
        log.error(f"Missing required environment variables: {', '.join(missing)}")
        log.error("Copy .env.example to .env and fill in your credentials.")
        return

    if not os.path.exists(CREDENTIALS_FILE):
        log.error(
            f"Google credentials file not found: {CREDENTIALS_FILE}\n"
            "See README: create a Service Account and download the JSON key."
        )
        return

    # ── Read sheet ────────────────────────────────────────────────────────────
    log.info("Connecting to Google Sheets…")
    try:
        sheet = get_worksheet()
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
    except Exception as exc:
        log.error(f"Could not read Google Sheet: {exc}")
        return

    if not records:
        log.info("Sheet is empty – nothing to send.")
        return

    sent_col = ensure_sent_column(sheet, headers)

    # ── Filter contacts to email ──────────────────────────────────────────────
    pending = []
    for row_idx, row in enumerate(records, start=2):
        email = str(row.get(COL_EMAIL, "")).strip()
        product = str(row.get(COL_PRODUCT, "nuestros productos")).strip() or "nuestros productos"
        already_sent = str(row.get(COL_SENT, "")).strip()

        if not email or "@" not in email:
            log.warning(f"Row {row_idx}: invalid email '{email}' – skipped.")
            continue
        if already_sent:
            continue

        pending.append((row_idx, email, product))

    if not pending:
        log.info("No new contacts to email.")
        return

    log.info(f"{len(pending)} contact(s) to process.")

    if dry_run:
        log.info("DRY RUN – no emails will be sent.")
        for _, email, product in pending:
            log.info(f"  Would send to: {email}  (product: {product})")
        return

    # ── Open SMTP and send ────────────────────────────────────────────────────
    log.info(f"Connecting to SMTP {SMTP_HOST}:{SMTP_PORT}…")
    try:
        smtp = open_smtp_connection()
    except Exception as exc:
        log.error(f"SMTP connection failed: {exc}")
        return

    sent_count = 0
    error_count = 0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    with smtp:
        for row_idx, email, product in pending:
            msg = build_email(email, product)
            try:
                smtp.sendmail(SMTP_USER, email, msg.as_string())
                sheet.update_cell(row_idx, sent_col, timestamp)
                log.info(f"  ✓  {email}  ({product})")
                sent_count += 1
            except Exception as exc:
                log.error(f"  ✗  {email}: {exc}")
                error_count += 1

    log.info(
        f"Finished. Sent: {sent_count}  |  Errors: {error_count}"
    )


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MineRun follow-up mailer")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be sent without actually sending.",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
