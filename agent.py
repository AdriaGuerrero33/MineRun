#!/usr/bin/env python3
"""
MineRun – Agente de seguimiento automático
===========================================
Corre en Railway (o en tu PC) en segundo plano.
Revisa el Google Sheet cada cierto intervalo y envía
un correo de seguimiento a cada contacto nuevo.

Railway: configura las variables de entorno en el dashboard.
Local:   usa el archivo .env y credentials.json.
"""

import json
import logging
import os
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# ── Rutas ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
SMTP_HOST        = os.getenv("SMTP_HOST", "correo.hostalia.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "25"))
SMTP_USER        = os.getenv("SMTP_USER", "")
SMTP_PASS        = os.getenv("SMTP_PASS", "")
FROM_NAME        = os.getenv("FROM_NAME", "Reseñas Plus")

SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_NAME       = os.getenv("GOOGLE_SHEET_NAME", "Emails")
CREDENTIALS_FILE = BASE_DIR / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

COL_EMAIL        = os.getenv("COL_EMAIL", "Email")
COL_PRODUCT      = os.getenv("COL_PRODUCT", "Producto")
COL_SENT         = "Enviado"

# Horas entre cada revisión del sheet (por defecto: cada 24 h)
CHECK_INTERVAL_H = int(os.getenv("CHECK_INTERVAL_HOURS", "24"))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Logging ───────────────────────────────────────────────────────────────────
log_file = BASE_DIR / "agent.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("MineRunAgent")


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────

def _google_credentials() -> Credentials:
    """
    Lee las credenciales de Google en este orden:
    1. Variable de entorno GOOGLE_CREDENTIALS_JSON (Railway)
    2. Archivo credentials.json local (PC)
    """
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    return Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=SCOPES)


def get_worksheet() -> gspread.Worksheet:
    client = gspread.authorize(_google_credentials())
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


def ensure_sent_column(sheet: gspread.Worksheet, headers: list) -> int:
    if COL_SENT in headers:
        return headers.index(COL_SENT) + 1
    new_col = len(headers) + 1
    sheet.update_cell(1, new_col, COL_SENT)
    log.info(f"Columna '{COL_SENT}' creada en posición {new_col}.")
    return new_col


def build_email(to_email: str, product: str) -> MIMEMultipart:
    subject = f"¿Sigues interesado/a en {product}?"
    body_text = f"""\
Hola,

Hace un tiempo mostraste interés en {product} y nos gustaría saber \
si podemos ayudarte a dar el siguiente paso.

En Reseñas Plus llevamos años ayudando a nuestros clientes a crecer \
con su reputación online y queremos hacer lo mismo por ti.

¿Tienes unos minutos para charlar? Responde a este correo o llámanos \
y lo resolvemos juntos.

Un saludo,
{FROM_NAME}

──────────────────────────────────────────────
Si no deseas recibir más correos, responde con
el asunto "BAJA" y te eliminamos de inmediato.
"""
    body_html = f"""\
<html><body style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:600px;">
  <p>Hola,</p>
  <p>Hace un tiempo mostraste interés en <strong>{product}</strong> y nos
  gustaría saber si podemos ayudarte a dar el siguiente paso.</p>
  <p>En <strong>Reseñas Plus</strong> llevamos años ayudando a nuestros clientes a
  crecer con su reputación online y queremos hacer lo mismo por ti.</p>
  <p>¿Tienes unos minutos para charlar? Responde a este correo o llámanos
  y lo resolvemos juntos.</p>
  <p>Un saludo,<br/><strong>{FROM_NAME}</strong></p>
  <hr style="border:none;border-top:1px solid #eee;margin-top:30px;"/>
  <p style="font-size:11px;color:#aaa;">
    Si no deseas recibir más correos, responde con el asunto
    <em>BAJA</em> y te eliminamos de inmediato.
  </p>
</body></html>
"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    return msg


def open_smtp():
    if SMTP_PORT == 465:
        conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    elif SMTP_PORT == 587:
        import ssl
        ctx = ssl.create_default_context()
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo(); conn.starttls(context=ctx); conn.ehlo()
    else:
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo()
    conn.login(SMTP_USER, SMTP_PASS)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Ciclo de envío
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle():
    """Un ciclo completo: leer sheet → enviar pendientes → marcar enviados."""
    log.info("── Iniciando ciclo ──────────────────────────")

    try:
        sheet   = get_worksheet()
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
    except Exception as exc:
        log.error(f"Error leyendo Google Sheet: {exc}")
        return

    if not records:
        log.info("Sheet vacío, nada que hacer.")
        return

    sent_col = ensure_sent_column(sheet, headers)

    pending = []
    for row_idx, row in enumerate(records, start=2):
        email        = str(row.get(COL_EMAIL, "")).strip()
        product      = str(row.get(COL_PRODUCT, "nuestros servicios")).strip() or "nuestros servicios"
        already_sent = str(row.get(COL_SENT, "")).strip()

        if not email or "@" not in email:
            continue
        if already_sent:
            continue
        pending.append((row_idx, email, product))

    if not pending:
        log.info("Sin contactos nuevos, hasta el próximo ciclo.")
        return

    log.info(f"{len(pending)} contacto(s) nuevos a enviar.")

    try:
        smtp = open_smtp()
    except Exception as exc:
        log.error(f"No se pudo conectar al SMTP: {exc}")
        return

    sent  = 0
    errors = 0
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    with smtp:
        for row_idx, email, product in pending:
            msg = build_email(email, product)
            try:
                smtp.sendmail(SMTP_USER, email, msg.as_string())
                sheet.update_cell(row_idx, sent_col, stamp)
                log.info(f"  ✓  {email}  [{product}]")
                sent += 1
            except Exception as exc:
                log.error(f"  ✗  {email}: {exc}")
                errors += 1

    log.info(f"Ciclo terminado — Enviados: {sent} | Errores: {errors}")


# ─────────────────────────────────────────────────────────────────────────────
# Bucle principal del agente
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("═══════════════════════════════════════════")
    log.info("  MineRun Agent arrancado")
    log.info(f"  Intervalo de revisión: cada {CHECK_INTERVAL_H} hora(s)")
    log.info(f"  Sheet: {SHEET_NAME}  |  SMTP: {SMTP_HOST}:{SMTP_PORT}")
    log.info("═══════════════════════════════════════════")

    while True:
        try:
            run_cycle()
        except Exception as exc:
            log.error(f"Error inesperado en el ciclo: {exc}")

        next_run = datetime.now().strftime("%Y-%m-%d") + f" (en {CHECK_INTERVAL_H}h)"
        log.info(f"Próxima revisión: {next_run}")
        time.sleep(CHECK_INTERVAL_H * 3600)


if __name__ == "__main__":
    main()
