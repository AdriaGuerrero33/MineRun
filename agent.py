#!/usr/bin/env python3
"""
Agente de Email Marketing
=========================
Corre 24/7 en Railway (o en tu PC).
Revisa el Google Sheet cada semana, envía seguimientos a contactos
nuevos y te manda un reporte por Telegram al terminar.

Railway: configura las variables de entorno en el dashboard.
Local:   usa el archivo .env y credentials.json.
"""

import json
import logging
import os
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# ── Rutas ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── Config SMTP ───────────────────────────────────────────────────────────────
SMTP_HOST        = os.getenv("SMTP_HOST", "correo.hostalia.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER        = os.getenv("SMTP_USER", "")
SMTP_PASS        = os.getenv("SMTP_PASS", "")
FROM_NAME        = os.getenv("FROM_NAME", "Reseñas Plus")

# ── Config Google Sheets ──────────────────────────────────────────────────────
SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_NAME       = os.getenv("GOOGLE_SHEET_NAME", "Emails")
CREDENTIALS_FILE = BASE_DIR / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
COL_EMAIL        = os.getenv("COL_EMAIL", "Email")
COL_PRODUCT      = os.getenv("COL_PRODUCT", "Producto")
COL_SENT         = "Enviado"

# ── Config Telegram ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Intervalo ─────────────────────────────────────────────────────────────────
CHECK_INTERVAL_H = int(os.getenv("CHECK_INTERVAL_HOURS", "168"))  # 1 semana

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
log = logging.getLogger("EmailMarketingAgent")


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as exc:
        log.warning(f"Telegram: no se pudo enviar el reporte: {exc}")


def build_report(sent: int, errors: int, total: int, already_sent: int, pending: int) -> str:
    now        = datetime.now()
    next_run   = now + timedelta(hours=CHECK_INTERVAL_H)
    fecha      = now.strftime("%d/%m/%Y %H:%M")
    fecha_next = next_run.strftime("%d/%m/%Y %H:%M")

    lines = [
        "📊 <b>Reporte semanal – Agente Email Marketing</b>",
        f"📅 {fecha}",
        "",
        f"✉️ Emails enviados esta semana: <b>{sent}</b>",
        f"❌ Errores de envío: <b>{errors}</b>",
        "",
        f"👥 Total contactos en el Sheet: <b>{total}</b>",
        f"✅ Ya contactados: <b>{already_sent}</b>",
        f"⏳ Pendientes de contactar: <b>{pending}</b>",
        "",
        f"🔜 Próxima revisión: {fecha_next}",
    ]
    if errors > 0:
        lines.append("")
        lines.append("⚠️ Hubo errores en algunos envíos. Revisa <code>agent.log</code>.")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────────────────────────────────────

def _google_credentials() -> Credentials:
    """Lee credenciales desde env var GOOGLE_CREDENTIALS_JSON (Railway)
    o desde el archivo credentials.json (local)."""
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
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


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def build_email(to_email: str, product: str) -> MIMEMultipart:
    subject   = f"¿Sigues interesado/a en {product}?"
    body_text = f"""\
Hola,

Hace un tiempo mostraste interés en {product} y nos gustaría saber \
si podemos ayudarte a dar el siguiente paso.

En {FROM_NAME} llevamos años ayudando a nuestros clientes a crecer \
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
  <p>En <strong>{FROM_NAME}</strong> llevamos años ayudando a nuestros clientes
  a crecer con su reputación online y queremos hacer lo mismo por ti.</p>
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
    msg            = MIMEMultipart("alternative")
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
        ctx  = ssl.create_default_context()
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo(); conn.starttls(context=ctx); conn.ehlo()
    else:
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        conn.ehlo()
    conn.login(SMTP_USER, SMTP_PASS)
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Ciclo principal
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle():
    log.info("── Iniciando ciclo ──────────────────────────")

    try:
        sheet   = get_worksheet()
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
    except Exception as exc:
        msg = f"Error leyendo Google Sheet: {exc}"
        log.error(msg)
        send_telegram(f"🚨 <b>Agente Email Marketing</b>\n{msg}")
        return

    total     = len(records)
    sent_col  = ensure_sent_column(sheet, headers)

    pending_rows   = []
    already_sent_n = 0

    for row_idx, row in enumerate(records, start=2):
        email        = str(row.get(COL_EMAIL, "")).strip()
        product      = str(row.get(COL_PRODUCT, "nuestros servicios")).strip() or "nuestros servicios"
        was_sent     = str(row.get(COL_SENT, "")).strip()

        if not email or "@" not in email:
            continue
        if was_sent:
            already_sent_n += 1
            continue
        pending_rows.append((row_idx, email, product))

    pending_n = len(pending_rows)

    if not pending_rows:
        log.info("Sin contactos nuevos.")
        send_telegram(build_report(0, 0, total, already_sent_n, 0))
        return

    log.info(f"{pending_n} contacto(s) nuevos a enviar.")

    try:
        smtp = open_smtp()
    except Exception as exc:
        msg = f"No se pudo conectar al SMTP: {exc}"
        log.error(msg)
        send_telegram(f"🚨 <b>Agente Email Marketing</b>\n{msg}")
        return

    sent   = 0
    errors = 0
    stamp  = datetime.now().strftime("%Y-%m-%d %H:%M")

    with smtp:
        for row_idx, email, product in pending_rows:
            msg_obj = build_email(email, product)
            try:
                smtp.sendmail(SMTP_USER, email, msg_obj.as_string())
                sheet.update_cell(row_idx, sent_col, stamp)
                log.info(f"  ✓  {email}  [{product}]")
                sent += 1
            except Exception as exc:
                log.error(f"  ✗  {email}: {exc}")
                errors += 1

    log.info(f"Ciclo terminado — Enviados: {sent} | Errores: {errors}")

    # Reporte Telegram
    send_telegram(build_report(
        sent       = sent,
        errors     = errors,
        total      = total,
        already_sent = already_sent_n + sent,
        pending    = pending_n - sent,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Entrada
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("═══════════════════════════════════════════════")
    log.info("  Agente de Email Marketing arrancado")
    log.info(f"  Revisión cada {CHECK_INTERVAL_H} hora(s)  |  SMTP {SMTP_HOST}:{SMTP_PORT}")
    log.info(f"  Telegram: {'configurado' if TELEGRAM_TOKEN else 'no configurado'}")
    log.info("═══════════════════════════════════════════════")

    send_telegram(
        "🚀 <b>Agente de Email Marketing arrancado</b>\n"
        f"Revisaré el Sheet cada <b>{CHECK_INTERVAL_H}h</b> y te enviaré un reporte aquí."
    )

    while True:
        try:
            run_cycle()
        except Exception as exc:
            log.error(f"Error inesperado: {exc}")
            send_telegram(f"🚨 <b>Error inesperado en el agente</b>\n<code>{exc}</code>")

        next_run = datetime.now() + timedelta(hours=CHECK_INTERVAL_H)
        log.info(f"Próxima revisión: {next_run.strftime('%Y-%m-%d %H:%M')}")
        time.sleep(CHECK_INTERVAL_H * 3600)


if __name__ == "__main__":
    main()
