#!/usr/bin/env python3
"""
Agente de Email Marketing
=========================
Corre 24/7 en Railway (o en tu PC).
Cada semana:
  1. Lee el Google Sheet y envía seguimientos a contactos nuevos.
  2. Revisa el buzón de entrada (IMAP) para ver quién ha contestado.
  3. Te manda un reporte de texto + nota de voz por Telegram.
"""

import email as emaillib
import imaplib
import io
import json
import logging
import os
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path

import requests
from dotenv import load_dotenv
from gtts import gTTS
import gspread
from google.oauth2.service_account import Credentials

# ── Rutas ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── Config SMTP / IMAP ────────────────────────────────────────────────────────
SMTP_HOST        = os.getenv("SMTP_HOST", "correo.hostalia.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER        = os.getenv("SMTP_USER", "")
SMTP_PASS        = os.getenv("SMTP_PASS", "")
FROM_NAME        = os.getenv("FROM_NAME", "Reseñas Plus")

IMAP_HOST        = os.getenv("IMAP_HOST", "217.116.0.237")
IMAP_PORT        = int(os.getenv("IMAP_PORT", "143"))

# ── Config Google Sheets ──────────────────────────────────────────────────────
SHEET_ID         = os.getenv("GOOGLE_SHEET_ID", "")
SHEET_NAME       = os.getenv("GOOGLE_SHEET_NAME", "Emails")
CREDENTIALS_FILE = BASE_DIR / os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
COL_EMAIL        = os.getenv("COL_EMAIL", "Email")
COL_PRODUCT      = os.getenv("COL_PRODUCT", "Producto")
COL_SENT         = "Enviado"
COL_REPLIED      = "Contestado"

# ── Config Telegram ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Intervalo ─────────────────────────────────────────────────────────────────
CHECK_INTERVAL_H = int(os.getenv("CHECK_INTERVAL_HOURS", "168"))

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
# Telegram helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tg(method: str, **kwargs) -> dict:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return {}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            timeout=15, **kwargs
        )
        return r.json()
    except Exception as exc:
        log.warning(f"Telegram {method} falló: {exc}")
        return {}


def send_telegram_text(text: str) -> None:
    _tg("sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})


def send_telegram_voice(text_es: str) -> None:
    """Genera audio en español con gTTS y lo envía como nota de voz."""
    try:
        tts = gTTS(text=text_es, lang="es", slow=False)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)
        _tg("sendVoice", data={"chat_id": TELEGRAM_CHAT_ID},
            files={"voice": ("reporte.mp3", buf, "audio/mpeg")})
        log.info("Nota de voz enviada por Telegram.")
    except Exception as exc:
        log.warning(f"No se pudo generar/enviar el audio: {exc}")
        send_telegram_text("⚠️ No se pudo generar la nota de voz. Revisa el log.")


# ─────────────────────────────────────────────────────────────────────────────
# IMAP – detección de respuestas
# ─────────────────────────────────────────────────────────────────────────────

def check_imap_replies(contacted_emails: set[str]) -> list[str]:
    """
    Conecta al buzón de entrada y devuelve la lista de emails
    de contactos que han respondido en los últimos 7 días.
    """
    replied = []
    if not contacted_emails:
        return replied

    since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")

    try:
        mail = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        mail.login(SMTP_USER, SMTP_PASS)
        mail.select("INBOX")

        _, data = mail.search(None, f'SINCE {since}')
        ids = data[0].split()
        log.info(f"IMAP: {len(ids)} emails en bandeja de entrada (últimos 7 días).")

        for num in ids:
            _, msg_data = mail.fetch(num, "(RFC822)")
            raw = msg_data[0][1]
            msg = emaillib.message_from_bytes(raw)
            from_raw = msg.get("From", "")
            _, from_addr = parseaddr(from_raw)
            from_addr = from_addr.lower().strip()
            if from_addr in contacted_emails:
                replied.append(from_addr)
                log.info(f"  Respuesta detectada de: {from_addr}")

        mail.logout()
    except Exception as exc:
        log.warning(f"IMAP check falló: {exc}")

    return replied


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────────────────────────────────────

def _google_credentials() -> Credentials:
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=SCOPES)


def get_worksheet() -> gspread.Worksheet:
    client = gspread.authorize(_google_credentials())
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


def ensure_column(sheet: gspread.Worksheet, headers: list, name: str) -> int:
    if name in headers:
        return headers.index(name) + 1
    new_col = len(headers) + 1
    sheet.update_cell(1, new_col, name)
    headers.append(name)
    log.info(f"Columna '{name}' creada en posición {new_col}.")
    return new_col


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def build_email(to_email: str, product: str) -> MIMEMultipart:
    subject   = f"¿Sigues interesado/a en {product}?"
    body_text = (
        f"Hola,\n\n"
        f"Hace un tiempo mostraste interés en {product} y nos gustaría saber "
        f"si podemos ayudarte a dar el siguiente paso.\n\n"
        f"En {FROM_NAME} llevamos años ayudando a nuestros clientes a crecer "
        f"con su reputación online y queremos hacer lo mismo por ti.\n\n"
        f"¿Tienes unos minutos para charlar? Responde a este correo o llámanos "
        f"y lo resolvemos juntos.\n\n"
        f"Un saludo,\n{FROM_NAME}\n\n"
        f"─────────────────────────────────────────────\n"
        f"Si no deseas recibir más correos, responde con\n"
        f'el asunto "BAJA" y te eliminamos de inmediato.\n'
    )
    body_html = (
        f'<html><body style="font-family:Arial,sans-serif;font-size:15px;color:#222;max-width:600px;">'
        f"<p>Hola,</p>"
        f"<p>Hace un tiempo mostraste interés en <strong>{product}</strong> y nos "
        f"gustaría saber si podemos ayudarte a dar el siguiente paso.</p>"
        f"<p>En <strong>{FROM_NAME}</strong> llevamos años ayudando a nuestros clientes "
        f"a crecer con su reputación online y queremos hacer lo mismo por ti.</p>"
        f"<p>¿Tienes unos minutos para charlar? Responde a este correo o llámanos "
        f"y lo resolvemos juntos.</p>"
        f"<p>Un saludo,<br/><strong>{FROM_NAME}</strong></p>"
        f'<hr style="border:none;border-top:1px solid #eee;margin-top:30px;"/>'
        f'<p style="font-size:11px;color:#aaa;">Si no deseas recibir más correos, '
        f"responde con el asunto <em>BAJA</em> y te eliminamos de inmediato.</p>"
        f"</body></html>"
    )
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
# Reporte de voz
# ─────────────────────────────────────────────────────────────────────────────

def build_voice_script(
    sent: int, errors: int,
    total: int, already_contacted: int, pending: int,
    new_replies: list[str], total_replied: int,
) -> str:
    now       = datetime.now()
    next_run  = now + timedelta(hours=CHECK_INTERVAL_H)
    tasa      = round((total_replied / already_contacted * 100) if already_contacted else 0)
    opor      = total_replied  # los que han contestado son oportunidades reales

    lines = [
        f"Hola, aquí tienes el reporte semanal de tu agente de email marketing. "
        f"Fecha del reporte: {now.strftime('%d de %B de %Y')}.",
        "",
        f"Esta semana hemos enviado {sent} emails de seguimiento a contactos nuevos. "
        + (f"Ha habido {errors} errores de envío." if errors else "Sin errores de envío."),
        "",
        f"Revisando tu bandeja de entrada, "
        + (
            f"hemos detectado {len(new_replies)} respuestas nuevas esta semana. "
            if new_replies else
            "no hemos detectado respuestas nuevas esta semana. "
        )
        + f"En total, {total_replied} contactos han respondido alguna vez, "
        f"lo que representa una tasa de respuesta del {tasa} por ciento.",
        "",
        f"Tenemos {opor} oportunidades reales identificadas: "
        f"son los contactos que han mostrado interés activo respondiendo a nuestros emails. "
        f"Te recomendamos hacer un seguimiento personal con ellos esta semana.",
        "",
        f"En cuanto al estado general de tu lista: "
        f"tienes {total} contactos en total, "
        f"{already_contacted} ya han recibido un seguimiento "
        f"y {pending} están pendientes de contactar.",
        "",
        f"La próxima revisión automática será el {next_run.strftime('%d de %B a las %H:%M')}. "
        f"¡Mucho éxito esta semana!",
    ]
    return " ".join(l for l in lines if l)


# ─────────────────────────────────────────────────────────────────────────────
# Ciclo principal
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle():
    log.info("── Iniciando ciclo ──────────────────────────")

    # ── Leer sheet ────────────────────────────────────────────────────────────
    try:
        sheet   = get_worksheet()
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
    except Exception as exc:
        msg = f"Error leyendo Google Sheet: {exc}"
        log.error(msg)
        send_telegram_text(f"🚨 <b>Agente Email Marketing</b>\n{msg}")
        return

    total     = len(records)
    sent_col  = ensure_column(sheet, headers, COL_SENT)
    reply_col = ensure_column(sheet, headers, COL_REPLIED)

    pending_rows     = []
    contacted_emails = set()
    already_replied  = set()

    for row_idx, row in enumerate(records, start=2):
        email_val   = str(row.get(COL_EMAIL, "")).strip().lower()
        product     = str(row.get(COL_PRODUCT, "nuestros servicios")).strip() or "nuestros servicios"
        was_sent    = str(row.get(COL_SENT, "")).strip()
        was_replied = str(row.get(COL_REPLIED, "")).strip()

        if not email_val or "@" not in email_val:
            continue
        if was_sent:
            contacted_emails.add(email_val)
        if was_replied:
            already_replied.add(email_val)
        if not was_sent:
            pending_rows.append((row_idx, email_val, product))

    # ── Enviar emails pendientes ──────────────────────────────────────────────
    sent   = 0
    errors = 0

    if pending_rows:
        log.info(f"{len(pending_rows)} contacto(s) nuevos a enviar.")
        try:
            smtp  = open_smtp()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with smtp:
                for row_idx, email_val, product in pending_rows:
                    msg_obj = build_email(email_val, product)
                    try:
                        smtp.sendmail(SMTP_USER, email_val, msg_obj.as_string())
                        sheet.update_cell(row_idx, sent_col, stamp)
                        contacted_emails.add(email_val)
                        log.info(f"  ✓  {email_val}  [{product}]")
                        sent += 1
                    except Exception as exc:
                        log.error(f"  ✗  {email_val}: {exc}")
                        errors += 1
        except Exception as exc:
            log.error(f"SMTP falló: {exc}")
            send_telegram_text(f"🚨 <b>Agente Email Marketing</b>\nSMTP error: {exc}")
    else:
        log.info("Sin contactos nuevos que enviar.")

    # ── Revisar respuestas por IMAP ───────────────────────────────────────────
    new_replies = check_imap_replies(contacted_emails - already_replied)

    stamp_reply = datetime.now().strftime("%Y-%m-%d %H:%M")
    for row_idx, row in enumerate(records, start=2):
        email_val = str(row.get(COL_EMAIL, "")).strip().lower()
        if email_val in new_replies:
            sheet.update_cell(row_idx, reply_col, stamp_reply)
            already_replied.add(email_val)
            log.info(f"  Marcado como contestado: {email_val}")

    # ── Estadísticas finales ──────────────────────────────────────────────────
    total_replied = len(already_replied)
    pending_final = len([r for r in records
                         if not str(r.get(COL_SENT, "")).strip()
                         and str(r.get(COL_EMAIL, "")).strip()])

    log.info(f"Ciclo terminado — Enviados: {sent} | Respuestas nuevas: {len(new_replies)} | "
             f"Total respondido: {total_replied}")

    # ── Reporte texto por Telegram ────────────────────────────────────────────
    text_report = (
        "📊 <b>Reporte semanal – Agente Email Marketing</b>\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"✉️ Emails enviados esta semana: <b>{sent}</b>\n"
        f"❌ Errores: <b>{errors}</b>\n\n"
        f"💬 Respuestas nuevas detectadas: <b>{len(new_replies)}</b>\n"
        f"🎯 Oportunidades reales (total respondido): <b>{total_replied}</b>\n\n"
        f"👥 Total contactos: <b>{total}</b>\n"
        f"✅ Ya contactados: <b>{len(contacted_emails)}</b>\n"
        f"⏳ Pendientes: <b>{pending_final}</b>\n\n"
        f"🔜 Próxima revisión: {(datetime.now() + timedelta(hours=CHECK_INTERVAL_H)).strftime('%d/%m/%Y %H:%M')}"
    )
    send_telegram_text(text_report)

    # ── Nota de voz ───────────────────────────────────────────────────────────
    voice_script = build_voice_script(
        sent=sent, errors=errors,
        total=total, already_contacted=len(contacted_emails),
        pending=pending_final,
        new_replies=new_replies, total_replied=total_replied,
    )
    send_telegram_voice(voice_script)


# ─────────────────────────────────────────────────────────────────────────────
# Entrada
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("═══════════════════════════════════════════════════")
    log.info("  Agente de Email Marketing arrancado")
    log.info(f"  Revisión cada {CHECK_INTERVAL_H}h  |  SMTP {SMTP_HOST}:{SMTP_PORT}")
    log.info(f"  IMAP {IMAP_HOST}:{IMAP_PORT}")
    log.info(f"  Telegram: {'configurado ✓' if TELEGRAM_TOKEN else 'no configurado'}")
    log.info("═══════════════════════════════════════════════════")

    send_telegram_text(
        "🚀 <b>Agente de Email Marketing arrancado</b>\n"
        f"Revisaré el Sheet cada <b>{CHECK_INTERVAL_H}h</b>, "
        f"detectaré respuestas automáticamente y te enviaré un reporte de texto y audio."
    )

    while True:
        try:
            run_cycle()
        except Exception as exc:
            log.error(f"Error inesperado: {exc}")
            send_telegram_text(f"🚨 <b>Error inesperado en el agente</b>\n<code>{exc}</code>")

        next_run = datetime.now() + timedelta(hours=CHECK_INTERVAL_H)
        log.info(f"Próxima revisión: {next_run.strftime('%Y-%m-%d %H:%M')}")
        time.sleep(CHECK_INTERVAL_H * 3600)


if __name__ == "__main__":
    main()
