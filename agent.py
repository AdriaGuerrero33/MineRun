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
import threading
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
SMTP_PORT        = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER        = os.getenv("SMTP_USER", "")
SMTP_PASS        = os.getenv("SMTP_PASS", "")
FROM_NAME        = os.getenv("FROM_NAME", "Reseñas Plus")
FROM_EMAIL       = os.getenv("FROM_EMAIL", SMTP_USER)

# ── Config Brevo API (alternativa a SMTP, no bloqueada por Railway) ───────────
BREVO_API_KEY    = os.getenv("BREVO_API_KEY", "")

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
# Chat bidireccional con Telegram
# ─────────────────────────────────────────────────────────────────────────────

# Estado compartido entre el ciclo principal y el hilo de chat
_state: dict = {
    "next_run":      None,
    "last_run":      None,
    "last_sent":     0,
    "last_errors":   0,
    "total":         0,
    "contacted":     0,
    "pending":       0,
    "total_replied": 0,
}

_HELP_TEXT = (
    "🤖 <b>Comandos disponibles:</b>\n\n"
    "/estado   – Ver estadísticas actuales\n"
    "/proxima  – Cuándo es la próxima revisión\n"
    "/que      – Qué hace este agente\n"
    "/ayuda    – Mostrar esta ayuda"
)

def _handle_message(text: str) -> None:
    cmd = text.strip().lower().split()[0] if text.strip() else ""

    if cmd in ("/estado", "/status"):
        s = _state
        lr = s["last_run"].strftime("%d/%m/%Y %H:%M") if s["last_run"] else "Aún no ha corrido"
        nr = s["next_run"].strftime("%d/%m/%Y %H:%M") if s["next_run"] else "Pendiente"
        reply = (
            "📊 <b>Estado actual del agente</b>\n\n"
            f"🕐 Última revisión: <b>{lr}</b>\n"
            f"🔜 Próxima revisión: <b>{nr}</b>\n\n"
            f"✉️ Emails enviados (último ciclo): <b>{s['last_sent']}</b>\n"
            f"❌ Errores (último ciclo): <b>{s['last_errors']}</b>\n\n"
            f"👥 Total contactos: <b>{s['total']}</b>\n"
            f"✅ Ya contactados: <b>{s['contacted']}</b>\n"
            f"⏳ Pendientes: <b>{s['pending']}</b>\n"
            f"💬 Han respondido: <b>{s['total_replied']}</b>"
        )

    elif cmd in ("/proxima", "/siguiente"):
        nr = _state["next_run"]
        if nr:
            diff = nr - datetime.now()
            h, m = divmod(int(diff.total_seconds() / 60), 60)
            reply = f"🔜 Próxima revisión: <b>{nr.strftime('%d/%m/%Y %H:%M')}</b>\n(en {h}h {m}min)"
        else:
            reply = "⏳ El agente aún no ha terminado su primer ciclo."

    elif cmd in ("/que", "/info"):
        reply = (
            "🤖 <b>¿Qué hago?</b>\n\n"
            "Soy un agente de email marketing automático. Cada semana:\n\n"
            "1️⃣ Leo tu Google Sheet y envío emails de seguimiento a contactos nuevos\n"
            "2️⃣ Reviso tu bandeja de entrada para detectar quién ha respondido\n"
            "3️⃣ Te mando este reporte con las estadísticas\n\n"
            f"⏱ Reviso cada <b>{CHECK_INTERVAL_H} horas</b>"
        )

    elif cmd in ("/ayuda", "/help", "/start"):
        reply = _HELP_TEXT

    else:
        reply = (
            "👋 Hola. Puedo informarte sobre mi actividad.\n\n"
            + _HELP_TEXT
        )

    send_telegram_text(reply)


def _polling_loop() -> None:
    """Hilo en segundo plano que escucha mensajes entrantes de Telegram."""
    offset = 0
    while True:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                json={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=40,
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue
                text = msg.get("text", "")
                if text:
                    log.info(f"Mensaje Telegram recibido: {text!r}")
                    _handle_message(text)
        except Exception as exc:
            log.warning(f"Telegram polling error: {exc}")
            time.sleep(5)


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
        if IMAP_PORT == 993:
            mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        else:
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
    subject = "¿Al final lo descartasteis o seguís interesados?"

    body_text = (
        f"Hola,\n\n"
        f"Hace un tiempo estuvisteis mirando lo de conseguir más reseñas en Google.\n\n"
        f"Y luego... nada.\n\n"
        f"No sé si al final lo descartasteis o simplemente se quedó pendiente.\n\n"
        f"Si ya no es algo que os interese, sin problema, me lo decís y no os molesto más.\n\n"
        f"Y si seguís pensando en ello, podemos hablar cinco minutos "
        f"y os cuento exactamente cómo funciona y qué resultados están consiguiendo "
        f"otros negocios como el vuestro.\n\n"
        f"Sin compromisos. Solo una conversación.\n\n"
        f"¿Seguís interesados?\n\n"
        f"Un saludo,\n\n"
        f"El equipo de {FROM_NAME}\n"
        f"+34 611 00 50 18 (WhatsApp y llamadas)\n\n"
        f"─────────────────────────────────────────────\n"
        f"Si no quieres saber más de nosotros, responde con el asunto \"BAJA\".\n"
    )

    body_html = (
        f'<html><body style="font-family:Georgia,serif;font-size:16px;color:#222;'
        f'max-width:580px;margin:0 auto;line-height:1.7;">'
        f"<p>Hola,</p>"
        f"<p>Hace un tiempo estuvisteis mirando lo de conseguir más reseñas en Google.</p>"
        f"<p>Y luego... nada.</p>"
        f"<p>No sé si al final lo descartasteis o simplemente se quedó pendiente.</p>"
        f"<p>Si ya no es algo que os interese, sin problema, me lo decís y no os molesto más.</p>"
        f"<p>Y si seguís pensando en ello, podemos hablar cinco minutos "
        f"y os cuento exactamente cómo funciona y qué resultados están consiguiendo "
        f"otros negocios como el vuestro.</p>"
        f"<p>Sin compromisos. Solo una conversación.</p>"
        f'<p style="font-weight:bold;">¿Seguís interesados?</p>'
        f"<p>Un saludo,</p>"
        f'<p><strong>El equipo de {FROM_NAME}</strong><br/>'
        f'<a href="https://wa.me/34611005018" style="color:#222;text-decoration:none;">'
        f"+34 611 00 50 18</a> "
        f'<span style="color:#999;font-size:13px;">(WhatsApp y llamadas)</span></p>'
        f'<hr style="border:none;border-top:1px solid #eee;margin-top:40px;"/>'
        f'<p style="font-size:11px;color:#bbb;">Si no quieres saber más de nosotros, '
        f"responde con el asunto <em>BAJA</em>.</p>"
        f"</body></html>"
    )
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    return msg


def send_email(to_email: str, msg: MIMEMultipart) -> None:
    """Envía un email usando Brevo API (HTTPS) o SMTP como fallback."""
    subject   = msg["Subject"]
    from_addr = FROM_EMAIL or SMTP_USER

    # Extraer cuerpos del mensaje MIME
    body_html = body_text = ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/html":
            body_html = part.get_payload(decode=True).decode("utf-8")
        elif ct == "text/plain":
            body_text = part.get_payload(decode=True).decode("utf-8")

    if BREVO_API_KEY:
        payload = {
            "sender":      {"name": FROM_NAME, "email": from_addr},
            "to":          [{"email": to_email}],
            "subject":     subject,
            "htmlContent": body_html,
            "textContent": body_text,
        }
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Brevo API error {resp.status_code}: {resp.text}")
        return

    # Fallback SMTP
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
    conn.sendmail(SMTP_USER, to_email, msg.as_string())
    conn.quit()


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
        if not BREVO_API_KEY and (not SMTP_USER or not SMTP_PASS):
            err = "No hay BREVO_API_KEY ni credenciales SMTP configuradas. Revisa las variables de entorno."
            log.error(err)
            send_telegram_text(f"🚨 <b>Agente Email Marketing</b>\n{err}")
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for row_idx, email_val, product in pending_rows:
            msg_obj = build_email(email_val, product)
            try:
                send_email(email_val, msg_obj)
                sheet.update_cell(row_idx, sent_col, stamp)
                contacted_emails.add(email_val)
                log.info(f"  ✓  {email_val}  [{product}]")
                sent += 1
            except Exception as exc:
                log.error(f"  ✗  {email_val}: {exc}")
                errors += 1
                send_telegram_text(f"🚨 <b>Agente Email Marketing</b>\nError enviando a {email_val}: {exc}")
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

    # Actualizar estado compartido para el chat
    _state["last_run"]      = datetime.now()
    _state["last_sent"]     = sent
    _state["last_errors"]   = errors
    _state["total"]         = total
    _state["contacted"]     = len(contacted_emails)
    _state["pending"]       = pending_final
    _state["total_replied"] = total_replied

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

    # Arrancar hilo de escucha de mensajes Telegram
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        t = threading.Thread(target=_polling_loop, daemon=True, name="tg-poll")
        t.start()
        log.info("Hilo de chat Telegram arrancado.")

    send_telegram_text(
        "🚀 <b>Agente de Email Marketing arrancado</b>\n"
        f"Revisaré el Sheet cada <b>{CHECK_INTERVAL_H}h</b>, "
        f"detectaré respuestas automáticamente y te enviaré un reporte de texto y audio.\n\n"
        "💬 Ya puedes escribirme. Prueba con /ayuda"
    )

    while True:
        try:
            run_cycle()
        except Exception as exc:
            log.error(f"Error inesperado: {exc}")
            send_telegram_text(f"🚨 <b>Error inesperado en el agente</b>\n<code>{exc}</code>")

        next_run = datetime.now() + timedelta(hours=CHECK_INTERVAL_H)
        _state["next_run"] = next_run
        log.info(f"Próxima revisión: {next_run.strftime('%Y-%m-%d %H:%M')}")
        time.sleep(CHECK_INTERVAL_H * 3600)


if __name__ == "__main__":
    main()
