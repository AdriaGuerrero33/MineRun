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

import re
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

# ── Config Groq IA ────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")

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
COL_SEQ          = "Secuencia"
COL_ESTADO       = "Estado"      # HOT | BAJA | Contestado

# ── Config Telegram ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Intervalo ─────────────────────────────────────────────────────────────────
CHECK_INTERVAL_H = int(os.getenv("CHECK_INTERVAL_HOURS", "12"))
MIN_DAYS_BETWEEN = int(os.getenv("MIN_DAYS_BETWEEN_EMAILS", "3"))

# ── Estado global ─────────────────────────────────────────────────────────────
_PAUSED = False

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def _valid_email(addr: str) -> bool:
    return bool(_EMAIL_RE.match(addr.strip()))

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


MAIN_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "📊 Estado",   "callback_data": "/estado"},
        {"text": "📤 Enviar",   "callback_data": "/enviar"},
    ], [
        {"text": "⏸ Pausar",   "callback_data": "/pausar"},
        {"text": "📋 Ayuda",    "callback_data": "/ayuda"},
    ], [
        {"text": "📋 Lista",    "callback_data": "/lista"},
        {"text": "📈 Análisis", "callback_data": "/analisis"},
    ]]
}


def _tg_menu(text: str) -> None:
    _tg("sendMessage", json={
        "chat_id":      TELEGRAM_CHAT_ID,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": MAIN_KEYBOARD,
    })


def _ai(prompt: str, system: str = "") -> str:
    if not GROQ_API_KEY:
        return ""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": "llama3-8b-8192", "messages": messages, "max_tokens": 600},
            timeout=20,
        )
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log.warning(f"Groq API error: {exc}")
        return ""


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
    "hot_leads":     0,
    "last_replies":  0,
}

_HELP_TEXT = (
    "🤖 <b>Comandos disponibles:</b>\n\n"
    "/estado            – Ver estadísticas actuales\n"
    "/proxima           – Cuándo es la próxima revisión\n"
    "/que               – Qué hace este agente\n"
    "/enviar            – Enviar ahora a toda la lista\n"
    "/enviar email@...  – Enviar ahora a un contacto concreto\n"
    "/reiniciar         – Resetear secuencia de todos los contactos\n"
    "/pausar            – Pausar envíos automáticos\n"
    "/reanudar          – Reanudar envíos automáticos\n"
    "/lista             – Ver todos los contactos con estado\n"
    "/detalle email@... – Ficha completa de un contacto\n"
    "/analisis          – Análisis IA de la campaña\n"
    "/ayuda             – Mostrar esta ayuda"
)


def _reset_sequences() -> None:
    """Borra Enviado y Secuencia de todos los contactos para empezar desde email #1."""
    send_telegram_text("⏳ Reiniciando secuencias...")
    try:
        sheet   = get_worksheet()
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
    except Exception as exc:
        send_telegram_text(f"🚨 Error leyendo el Sheet: {exc}")
        return

    sent_col = ensure_column(sheet, headers, COL_SENT)
    seq_col  = ensure_column(sheet, headers, COL_SEQ)

    reset = 0
    for row_idx, row in enumerate(records, start=2):
        email_val = str(row.get(COL_EMAIL, "")).strip().lower()
        if not email_val or "@" not in email_val:
            continue
        sheet.update_cell(row_idx, sent_col, "")
        sheet.update_cell(row_idx, seq_col, "")
        reset += 1

    send_telegram_text(f"✅ <b>Reinicio completado</b>\n\n{reset} contactos reseteados al email #1.")


def _manual_send(target_email: str | None = None) -> None:
    """Envía el siguiente email de la secuencia a un contacto o a toda la lista."""
    send_telegram_text("⏳ Procesando envío manual...")
    try:
        sheet   = get_worksheet()
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
    except Exception as exc:
        send_telegram_text(f"🚨 Error leyendo el Sheet: {exc}")
        return

    sent_col  = ensure_column(sheet, headers, COL_SENT)
    seq_col   = ensure_column(sheet, headers, COL_SEQ)
    reply_col = ensure_column(sheet, headers, COL_REPLIED)

    stamp      = datetime.now().strftime("%Y-%m-%d %H:%M")
    sent       = 0
    errors     = 0
    skipped    = 0
    first_err  = ""

    for row_idx, row in enumerate(records, start=2):
        email_val   = str(row.get(COL_EMAIL, "")).strip().lower()
        was_replied = str(row.get(COL_REPLIED, "")).strip()
        seq         = int(str(row.get(COL_SEQ, "") or "0"))

        if not email_val or "@" not in email_val:
            continue
        if target_email and email_val != target_email.lower():
            continue
        if was_replied:
            skipped += 1
            continue

        seq_next = seq + 1
        msg_obj  = build_email(email_val, "", seq_next)
        try:
            send_email(email_val, msg_obj)
            sheet.update_cell(row_idx, sent_col, stamp)
            sheet.update_cell(row_idx, seq_col, seq_next)
            log.info(f"  [manual] ✓  {email_val}  [email #{seq_next}]")
            sent += 1
            time.sleep(1)   # 1 email/segundo para no superar el rate limit de Brevo
        except Exception as exc:
            log.error(f"  [manual] ✗  {email_val}: {exc}")
            if not first_err:
                first_err = str(exc)
            errors += 1

    if target_email and sent == 0 and errors == 0:
        send_telegram_text(f"⚠️ No encontré el contacto <code>{target_email}</code> en el Sheet o ya respondió.")
        return

    resumen = (
        f"✅ <b>Envío manual completado</b>\n\n"
        f"✉️ Enviados: <b>{sent}</b>\n"
        f"❌ Errores: <b>{errors}</b>\n"
        + (f"⏭ Omitidos (ya respondieron): <b>{skipped}</b>\n" if skipped else "")
        + (f"\n⚠️ Primer error: <code>{first_err}</code>" if first_err else "")
    )
    send_telegram_text(resumen)


def _show_detalle(target: str) -> None:
    try:
        sheet   = get_worksheet()
        records = sheet.get_all_records()
    except Exception as exc:
        send_telegram_text(f"🚨 Error leyendo el Sheet: {exc}")
        return
    for row in records:
        email_val = str(row.get(COL_EMAIL, "")).strip().lower()
        if email_val != target.lower().strip():
            continue
        seq      = str(row.get(COL_SEQ, "0") or "0")
        sent_ts  = str(row.get(COL_SENT, "")).strip() or "Nunca"
        replied  = str(row.get(COL_REPLIED, "")).strip() or "No"
        estado   = str(row.get(COL_ESTADO, "")).strip() or "—"
        tmpl_num = (int(seq or "0") % 6) + 1
        send_telegram_text(
            f"📋 <b>{email_val}</b>\n\n"
            f"Secuencia: email #{seq} (próximo: template {tmpl_num})\n"
            f"Último envío: {sent_ts}\n"
            f"Estado: {estado}\n"
            f"Contestado: {replied}"
        )
        return
    send_telegram_text(f"⚠️ No encontré el contacto <code>{target}</code> en el Sheet.")


def _show_lista() -> None:
    try:
        sheet   = get_worksheet()
        records = sheet.get_all_records()
    except Exception as exc:
        send_telegram_text(f"🚨 Error leyendo el Sheet: {exc}")
        return
    lines = []
    for row in records:
        email_val = str(row.get(COL_EMAIL, "")).strip().lower()
        if not email_val or "@" not in email_val:
            continue
        seq     = int(str(row.get(COL_SEQ, "0") or "0"))
        estado  = str(row.get(COL_ESTADO, "")).strip()
        replied = str(row.get(COL_REPLIED, "")).strip()
        if estado == "HOT":
            icon = "🔥"
        elif estado == "BAJA":
            icon = "🚫"
        elif replied:
            icon = "💬"
        elif seq >= 1:
            icon = "📤"
        else:
            icon = "⏳"
        label = estado if estado else (f"email #{seq}" if seq >= 1 else "pendiente")
        lines.append(f"{icon} {email_val} — {label}")
    if not lines:
        send_telegram_text("📋 No hay contactos en el Sheet.")
        return
    total = len(lines)
    for i in range(0, total, 20):
        chunk = lines[i:i + 20]
        send_telegram_text(f"📋 <b>Contactos ({i+1}–{min(i+20, total)} de {total})</b>\n\n" + "\n".join(chunk))


def _show_analisis() -> None:
    if not GROQ_API_KEY:
        send_telegram_text("⚠️ GROQ_API_KEY no configurada.")
        return
    send_telegram_text("🤖 Analizando campaña con IA...")
    s = _state
    prompt = (
        f"Analiza esta campaña de email marketing para reseñas en Google:\n"
        f"- Total: {s['total']} contactos\n"
        f"- Contactados: {s['contacted']}\n"
        f"- Pendientes: {s['pending']}\n"
        f"- Respondieron: {s['total_replied']}\n"
        f"- HOT leads: {s.get('hot_leads', 0)}\n"
        f"- Enviados último ciclo: {s['last_sent']}\n"
        f"- Errores último ciclo: {s['last_errors']}\n\n"
        f"Da 3-4 insights accionables en español. Sé conciso."
    )
    analysis = _ai(prompt, system="Eres experto en email marketing B2B. Da recomendaciones concretas en español.")
    if analysis:
        send_telegram_text(f"📈 <b>Análisis IA</b>\n\n{analysis}")
    else:
        send_telegram_text("⚠️ No se pudo obtener análisis de la IA.")


def _handle_message(text: str, callback_id: str | None = None) -> None:
    global _PAUSED
    parts = text.strip().split()
    cmd   = parts[0].lower() if parts else ""

    if callback_id:
        _tg("answerCallbackQuery", json={"callback_query_id": callback_id})

    if cmd in ("/estado", "/status"):
        s = _state
        lr = s["last_run"].strftime("%d/%m/%Y %H:%M") if s["last_run"] else "Aún no ha corrido"
        nr = s["next_run"].strftime("%d/%m/%Y %H:%M") if s["next_run"] else "Pendiente"
        pausa = " ⏸ <b>PAUSADO</b>" if _PAUSED else ""
        reply = (
            f"📊 <b>Estado actual del agente</b>{pausa}\n\n"
            f"🕐 Última revisión: <b>{lr}</b>\n"
            f"🔜 Próxima revisión: <b>{nr}</b>\n\n"
            f"✉️ Emails enviados (último ciclo): <b>{s['last_sent']}</b>\n"
            f"❌ Errores (último ciclo): <b>{s['last_errors']}</b>\n\n"
            f"👥 Total contactos: <b>{s['total']}</b>\n"
            f"✅ Ya contactados: <b>{s['contacted']}</b>\n"
            f"⏳ Pendientes: <b>{s['pending']}</b>\n"
            f"💬 Han respondido: <b>{s['total_replied']}</b>\n"
            f"🔥 Leads calientes: <b>{s.get('hot_leads', 0)}</b>"
        )
        send_telegram_text(reply)

    elif cmd in ("/proxima", "/siguiente"):
        nr = _state["next_run"]
        if nr:
            diff = nr - datetime.now()
            h, m = divmod(int(diff.total_seconds() / 60), 60)
            reply = f"🔜 Próxima revisión: <b>{nr.strftime('%d/%m/%Y %H:%M')}</b>\n(en {h}h {m}min)"
        else:
            reply = "⏳ El agente aún no ha terminado su primer ciclo."
        send_telegram_text(reply)

    elif cmd in ("/que", "/info"):
        send_telegram_text(
            "🤖 <b>¿Qué hago?</b>\n\n"
            "Soy un agente de email marketing automático. Los lunes y jueves:\n\n"
            "1️⃣ Leo tu Google Sheet y envío el siguiente email de la secuencia\n"
            "2️⃣ Reviso la bandeja de entrada y clasifico respuestas con IA (HOT/BAJA)\n"
            "3️⃣ Te mando reporte de texto y audio\n\n"
            f"⏱ Reviso cada <b>{CHECK_INTERVAL_H} horas</b>"
        )

    elif cmd == "/reiniciar":
        threading.Thread(target=_reset_sequences, daemon=True, name="reset").start()

    elif cmd == "/enviar":
        target = parts[1] if len(parts) > 1 else None
        threading.Thread(target=_manual_send, args=(target,), daemon=True, name="manual-send").start()

    elif cmd == "/pausar":
        _PAUSED = True
        send_telegram_text("⏸ <b>Envíos pausados.</b>\nUsa /reanudar para volver a activar.")

    elif cmd == "/reanudar":
        _PAUSED = False
        send_telegram_text("▶️ <b>Envíos reanudados.</b>")

    elif cmd == "/detalle":
        if len(parts) > 1:
            threading.Thread(target=_show_detalle, args=(parts[1],), daemon=True).start()
        else:
            send_telegram_text("⚠️ Uso: /detalle email@ejemplo.com")

    elif cmd == "/lista":
        threading.Thread(target=_show_lista, daemon=True).start()

    elif cmd == "/analisis":
        threading.Thread(target=_show_analisis, daemon=True).start()

    elif cmd in ("/ayuda", "/help", "/start"):
        _tg_menu(_HELP_TEXT)

    else:
        if GROQ_API_KEY and text and not text.startswith("/"):
            s = _state
            context = (
                f"Eres el asistente de ventas de Reseñas Plus, un servicio de reseñas en Google para negocios. "
                f"Gestionas una campaña de email marketing. "
                f"Estado: {s['total']} contactos, {s['contacted']} contactados, "
                f"{s['pending']} pendientes, {s['total_replied']} respondieron, "
                f"{s.get('hot_leads', 0)} leads calientes. "
                f"Responde en español de forma concisa y útil."
            )
            response = _ai(text, system=context)
            if response:
                send_telegram_text(f"🤖 {response}")
                return
        send_telegram_text("👋 Hola. Puedo informarte sobre mi actividad.\n\n" + _HELP_TEXT)


def _polling_loop() -> None:
    offset = 0
    while True:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                json={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
                timeout=40,
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    msg = update["message"]
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != str(TELEGRAM_CHAT_ID):
                        continue
                    text = msg.get("text", "")
                    if text:
                        log.info(f"Mensaje Telegram: {text!r}")
                        _handle_message(text)
                elif "callback_query" in update:
                    cq = update["callback_query"]
                    chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                    if chat_id != str(TELEGRAM_CHAT_ID):
                        continue
                    data_text = cq.get("data", "")
                    cb_id     = cq.get("id", "")
                    if data_text:
                        log.info(f"Callback Telegram: {data_text!r}")
                        _handle_message(data_text, callback_id=cb_id)
        except Exception as exc:
            log.warning(f"Telegram polling error: {exc}")
            time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# IMAP – detección de respuestas
# ─────────────────────────────────────────────────────────────────────────────

def _get_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""


def check_imap_replies(contacted_emails: set) -> dict:
    replies = {}
    if not contacted_emails:
        return replies
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
        log.info(f"IMAP: {len(ids)} emails en bandeja (últimos 7 días).")
        for num in ids:
            _, msg_data = mail.fetch(num, "(RFC822)")
            raw = msg_data[0][1]
            msg = emaillib.message_from_bytes(raw)
            from_raw = msg.get("From", "")
            _, from_addr = parseaddr(from_raw)
            from_addr = from_addr.lower().strip()
            if from_addr not in contacted_emails:
                continue
            subject = msg.get("Subject", "").lower()
            body    = _get_body(msg).lower()
            snippet = (subject + " " + body[:300]).strip()
            estado = "Contestado"
            if GROQ_API_KEY:
                clasificacion = _ai(
                    f"Clasifica esta respuesta de email: HOT, BAJA o NEUTRAL.\n"
                    f"HOT=interesado. BAJA=no quiere más emails. NEUTRAL=otro.\n"
                    f"Texto: {snippet}\nResponde solo la palabra:",
                    system="Clasificador de emails. Responde solo: HOT, BAJA o NEUTRAL.",
                )
                word = clasificacion.strip().upper().split()[0] if clasificacion else "NEUTRAL"
                if word in ("HOT", "BAJA"):
                    estado = word
            else:
                combined = subject + " " + body
                if any(k in combined for k in ["baja", "no me interesa", "desuscrib", "elimina", "no gracias", "stop"]):
                    estado = "BAJA"
                elif any(k in combined for k in ["sí", "si ", "interesa", "cuánto", "precio", "llamada", "info"]):
                    estado = "HOT"
            replies[from_addr] = estado
            log.info(f"  Respuesta de {from_addr}: {estado}")
        mail.logout()
    except Exception as exc:
        log.warning(f"IMAP check falló: {exc}")
    return replies


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
# Helpers de calendario y fecha
# ─────────────────────────────────────────────────────────────────────────────

def is_send_day() -> bool:
    """Devuelve True si hoy es lunes (0) o jueves (3)."""
    return datetime.now().weekday() in (0, 3)


def days_since(date_str: str) -> int:
    """Días transcurridos desde la fecha almacenada en el sheet."""
    if not date_str:
        return 999
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M")
        return (datetime.now() - d).days
    except Exception:
        return 999


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def build_email(to_email: str, product: str, seq: int = 1) -> MIMEMultipart:
    # Cicla entre 6 templates indefinidamente
    template = ((seq - 1) % 6) + 1

    if template == 1:
        # Re-engagement: ¿siguen interesados?
        subject = "¿Al final lo descartasteis o seguís interesados?"
        body_text = (
            "Hola,\n\n"
            "Hace un tiempo estuvisteis mirando lo de conseguir más reseñas en Google.\n\n"
            "Y luego... nada.\n\n"
            "No sé si al final lo descartasteis o simplemente se quedó pendiente.\n\n"
            "Si ya no es algo que os interese, sin problema, me lo decís y no os molesto más.\n\n"
            "Y si seguís pensando en ello, podemos hablar cinco minutos "
            "y os cuento exactamente cómo funciona y qué resultados están consiguiendo "
            "otros negocios como el vuestro.\n\n"
            "¿Seguís interesados?\n\n"
        )
        body_html = (
            "<p>Hola,</p>"
            "<p>Hace un tiempo estuvisteis mirando lo de conseguir más reseñas en Google.</p>"
            "<p>Y luego... nada.</p>"
            "<p>No sé si al final lo descartasteis o simplemente se quedó pendiente.</p>"
            "<p>Si ya no es algo que os interese, sin problema, me lo decís y no os molesto más.</p>"
            "<p>Y si seguís pensando en ello, podemos hablar cinco minutos "
            "y os cuento exactamente cómo funciona y qué resultados están consiguiendo "
            "otros negocios como el vuestro.</p>"
            '<p style="font-weight:bold;">¿Seguís interesados?</p>'
        )

    elif template == 2:
        # Prueba social: resultados reales
        subject = "Lo que están consiguiendo otros negocios como el vuestro"
        body_text = (
            "Hola,\n\n"
            "Una cosa rápida.\n\n"
            "Un restaurante con el que trabajamos pasó de 38 a 140 reseñas en Google en 45 días.\n\n"
            "Una clínica dental empezó a aparecer en el top 3 de Google Maps en su zona. Antes ni salía.\n\n"
            "No es magia. Es un sistema que funciona porque los clientes "
            "que ya están contentos simplemente no saben que pueden ayudaros con una reseña.\n\n"
            "Nosotros les recordamos por vosotros.\n\n"
            "¿Tiene sentido hablar diez minutos para ver si os encaja?\n\n"
        )
        body_html = (
            "<p>Hola,</p>"
            "<p>Una cosa rápida.</p>"
            "<p>Un restaurante con el que trabajamos pasó de <strong>38 a 140 reseñas</strong> "
            "en Google en 45 días.</p>"
            "<p>Una clínica dental empezó a aparecer en el <strong>top 3 de Google Maps</strong> "
            "en su zona. Antes ni salía.</p>"
            "<p>No es magia. Es un sistema que funciona porque los clientes "
            "que ya están contentos simplemente no saben que pueden ayudaros con una reseña.</p>"
            "<p>Nosotros les recordamos por vosotros.</p>"
            '<p style="font-weight:bold;">¿Tiene sentido hablar diez minutos para ver si os encaja?</p>'
        )

    elif template == 3:
        # Pregunta sobre su situación actual (Voss: pregunta calibrada)
        subject = "Una pregunta sobre vuestras reseñas"
        body_text = (
            "Hola,\n\n"
            "Curiosidad genuina.\n\n"
            "¿Cuántas reseñas en Google tenéis ahora mismo?\n\n"
            "Lo pregunto porque hay un umbral a partir del cual Google empieza "
            "a mostraros de forma consistente cuando alguien busca en vuestra zona.\n\n"
            "La mayoría de negocios están por debajo sin saberlo.\n\n"
            "Si me lo decís, os digo en dos minutos si estáis bien o si hay margen de mejora.\n\n"
            "Sin compromiso ninguno.\n\n"
        )
        body_html = (
            "<p>Hola,</p>"
            "<p>Curiosidad genuina.</p>"
            "<p>¿Cuántas reseñas en Google tenéis ahora mismo?</p>"
            "<p>Lo pregunto porque hay un umbral a partir del cual Google empieza "
            "a mostraros de forma consistente cuando alguien busca en vuestra zona.</p>"
            "<p>La mayoría de negocios están por debajo sin saberlo.</p>"
            "<p>Si me lo decís, os digo en dos minutos si estáis bien o si hay margen de mejora.</p>"
            "<p>Sin compromiso ninguno.</p>"
        )

    elif template == 4:
        # El coste de no actuar (Hormozi: value equation)
        subject = "¿Sabéis cuánto os cuesta no tener reseñas?"
        body_text = (
            "Hola,\n\n"
            "Un dato que suele sorprender a la gente.\n\n"
            "El 93% de los consumidores leen reseñas antes de elegir un negocio local.\n\n"
            "Y el 68% forma su opinión después de leer entre 1 y 6 reseñas.\n\n"
            "Lo que significa que si vuestra competencia tiene más reseñas que vosotros, "
            "os están quitando clientes cada día sin que os enteréis.\n\n"
            "No os lo cuento para asustaros. Os lo cuento porque tiene solución "
            "y es más sencilla de lo que parece.\n\n"
            "¿Hablamos?\n\n"
        )
        body_html = (
            "<p>Hola,</p>"
            "<p>Un dato que suele sorprender a la gente.</p>"
            "<p>El <strong>93% de los consumidores</strong> leen reseñas antes de elegir un negocio local.</p>"
            "<p>Y el <strong>68%</strong> forma su opinión después de leer entre 1 y 6 reseñas.</p>"
            "<p>Lo que significa que si vuestra competencia tiene más reseñas que vosotros, "
            "os están quitando clientes cada día sin que os enteréis.</p>"
            "<p>No os lo cuento para asustaros. Os lo cuento porque tiene solución "
            "y es más sencilla de lo que parece.</p>"
            '<p style="font-weight:bold;">¿Hablamos?</p>'
        )

    elif template == 5:
        # Historia / storytelling (Luis Monge Malo style)
        subject = "Lo que me dijo un cliente la semana pasada"
        body_text = (
            "Hola,\n\n"
            "La semana pasada un cliente nos llamó para contarnos algo.\n\n"
            "Nos dijo que desde que empezó a tener más reseñas en Google, "
            "la gente llega a su negocio ya convencida.\n\n"
            "Textualmente: \"Ya no tengo que venderme tanto. Llegan y me dicen "
            "que han leído las reseñas y que quieren trabajar conmigo.\"\n\n"
            "Eso es lo que hacen las reseñas. No son solo estrellas en Google. "
            "Son ventas que pasan sin que tengas que hacer nada.\n\n"
            "¿Os gustaría que os pasara lo mismo?\n\n"
        )
        body_html = (
            "<p>Hola,</p>"
            "<p>La semana pasada un cliente nos llamó para contarnos algo.</p>"
            "<p>Nos dijo que desde que empezó a tener más reseñas en Google, "
            "la gente llega a su negocio ya convencida.</p>"
            "<p>Textualmente: <em>\"Ya no tengo que venderme tanto. Llegan y me dicen "
            "que han leído las reseñas y que quieren trabajar conmigo.\"</em></p>"
            "<p>Eso es lo que hacen las reseñas. No son solo estrellas en Google. "
            "Son ventas que pasan sin que tengas que hacer nada.</p>"
            '<p style="font-weight:bold;">¿Os gustaría que os pasara lo mismo?</p>'
        )

    else:
        # template == 6: Soft re-engagement (Isra Bravo: directo y sin drama)
        subject = "¿Seguimos en contacto o lo dejamos?"
        body_text = (
            "Hola,\n\n"
            "Llevo un tiempo escribiéndoos sobre las reseñas en Google.\n\n"
            "No quiero ser pesado.\n\n"
            "Así que os pregunto directamente: ¿tiene sentido que sigamos en contacto "
            "o preferís que no os escriba más?\n\n"
            "Las dos opciones están bien.\n\n"
            "Si queréis que os deje en paz, responded con BAJA y listo, sin dramas.\n\n"
            "Y si en algún momento queréis retomar la conversación, "
            "aquí estaremos.\n\n"
        )
        body_html = (
            "<p>Hola,</p>"
            "<p>Llevo un tiempo escribiéndoos sobre las reseñas en Google.</p>"
            "<p>No quiero ser pesado.</p>"
            "<p>Así que os pregunto directamente: ¿tiene sentido que sigamos en contacto "
            "o preferís que no os escriba más?</p>"
            "<p>Las dos opciones están bien.</p>"
            "<p>Si queréis que os deje en paz, responded con <strong>BAJA</strong> y listo, sin dramas.</p>"
            "<p>Y si en algún momento queréis retomar la conversación, aquí estaremos.</p>"
        )

    # ── Firma común ───────────────────────────────────────────────────────────
    firma_text = (
        f"Un saludo,\n\n"
        f"El equipo de {FROM_NAME}\n"
        f"+34 611 00 50 18 (WhatsApp y llamadas)\n\n"
        f"─────────────────────────────────────────────\n"
        f"Si no quieres saber más de nosotros, responde con el asunto \"BAJA\".\n"
    )
    firma_html = (
        f"<p>Un saludo,</p>"
        f'<p><strong>El equipo de {FROM_NAME}</strong><br/>'
        f'<a href="https://wa.me/34611005018" style="color:#222;text-decoration:none;">'
        f"+34 611 00 50 18</a> "
        f'<span style="color:#999;font-size:13px;">(WhatsApp y llamadas)</span></p>'
        f'<hr style="border:none;border-top:1px solid #eee;margin-top:40px;"/>'
        f'<p style="font-size:11px;color:#bbb;">Si no quieres saber más de nosotros, '
        f"responde con el asunto <em>BAJA</em>.</p>"
    )

    full_html = (
        f'<html><body style="font-family:Georgia,serif;font-size:16px;color:#222;'
        f'max-width:580px;margin:0 auto;line-height:1.7;">'
        + body_html + firma_html +
        f"</body></html>"
    )
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(body_text + firma_text, "plain", "utf-8"))
    msg.attach(MIMEText(full_html, "html", "utf-8"))
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
    new_replies: int, total_replied: int,
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
            f"hemos detectado {new_replies} respuestas nuevas esta semana. "
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


def _next_send_day() -> str:
    now = datetime.now()
    day_names = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    for delta in range(1, 8):
        candidate = now + timedelta(days=delta)
        if candidate.weekday() in (0, 3):
            if delta == 1:
                return "mañana"
            return f"el {day_names[candidate.weekday()]}"
    return "pronto"


def _briefing_loop() -> None:
    while True:
        now    = datetime.now()
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        s = _state
        send_telegram_text(
            "☀️ <b>Buenos días — Resumen de ayer</b>\n\n"
            f"✉️ Enviados: <b>{s['last_sent']}</b>\n"
            f"🔥 Leads calientes: <b>{s.get('hot_leads', 0)}</b>\n"
            f"💬 Respuestas nuevas: <b>{s.get('last_replies', 0)}</b>\n"
            f"⏳ Pendientes en lista: <b>{s['pending']}</b>\n\n"
            f"📅 Próximo envío: {_next_send_day()}"
        )


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

    total      = len(records)
    sent_col   = ensure_column(sheet, headers, COL_SENT)
    reply_col  = ensure_column(sheet, headers, COL_REPLIED)
    seq_col    = ensure_column(sheet, headers, COL_SEQ)
    estado_col = ensure_column(sheet, headers, COL_ESTADO)

    hoy_es_dia_envio = is_send_day()
    log.info(f"Hoy es {'lunes/jueves ✓ — se enviarán emails' if hoy_es_dia_envio else 'día de solo monitorización (no se envían emails)'}.")

    pending_rows     = []   # (row_idx, email, product, seq_next)
    contacted_emails = set()
    already_replied  = set()

    for row_idx, row in enumerate(records, start=2):
        email_val   = str(row.get(COL_EMAIL, "")).strip().lower()
        was_sent    = str(row.get(COL_SENT, "")).strip()
        was_replied = str(row.get(COL_REPLIED, "")).strip()
        seq         = int(str(row.get(COL_SEQ, "") or "0"))

        if not email_val or "@" not in email_val:
            continue
        if seq >= 1:
            contacted_emails.add(email_val)
        if was_replied:
            already_replied.add(email_val)

        # Solo calcular pendientes si hoy toca enviar
        if not hoy_es_dia_envio:
            continue
        # No enviar más si ya contestó o se dio de baja
        if was_replied:
            continue
        if str(row.get(COL_ESTADO, "")).strip() == "BAJA":
            continue
        # El siguiente email requiere MIN_DAYS_BETWEEN días desde el último
        if seq >= 1 and days_since(was_sent) < MIN_DAYS_BETWEEN:
            continue

        pending_rows.append((row_idx, email_val, seq + 1))

    # ── Enviar emails pendientes ──────────────────────────────────────────────
    sent   = 0
    errors = 0

    if pending_rows and hoy_es_dia_envio:
        log.info(f"{len(pending_rows)} contacto(s) a enviar hoy.")
        if not BREVO_API_KEY and (not SMTP_USER or not SMTP_PASS):
            err = "No hay BREVO_API_KEY ni credenciales SMTP configuradas."
            log.error(err)
            send_telegram_text(f"🚨 <b>Agente Email Marketing</b>\n{err}")
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for row_idx, email_val, seq_next in pending_rows:
            msg_obj = build_email(email_val, "", seq_next)
            try:
                send_email(email_val, msg_obj)
                sheet.update_cell(row_idx, sent_col, stamp)
                sheet.update_cell(row_idx, seq_col, seq_next)
                contacted_emails.add(email_val)
                log.info(f"  ✓  {email_val}  [email #{seq_next}]")
                sent += 1
                time.sleep(1)   # respetar rate limit de Brevo
            except Exception as exc:
                log.error(f"  ✗  {email_val}: {exc}")
                errors += 1
                send_telegram_text(f"🚨 <b>Agente Email Marketing</b>\nError enviando a {email_val}: {exc}")
    elif not hoy_es_dia_envio:
        log.info("No es lunes ni jueves — no se envían emails hoy.")
    else:
        log.info("Sin contactos pendientes que enviar.")

    # ── Revisar respuestas por IMAP ───────────────────────────────────────────
    new_replies_dict = check_imap_replies(contacted_emails - already_replied)
    hot_count   = 0
    reply_count = len(new_replies_dict)

    stamp_reply = datetime.now().strftime("%Y-%m-%d %H:%M")
    for row_idx, row in enumerate(records, start=2):
        email_val = str(row.get(COL_EMAIL, "")).strip().lower()
        if email_val not in new_replies_dict:
            continue
        estado_nuevo = new_replies_dict[email_val]
        sheet.update_cell(row_idx, reply_col, stamp_reply)
        sheet.update_cell(row_idx, estado_col, estado_nuevo)
        already_replied.add(email_val)
        log.info(f"  Marcado {email_val} → {estado_nuevo}")
        if estado_nuevo == "HOT":
            hot_count += 1
            sugerencia = ""
            if GROQ_API_KEY:
                s_raw = _ai(
                    "Un lead interesado en reseñas Google respondió a un email de ventas. "
                    "Genera un borrador corto de respuesta en español para confirmar una llamada. Máximo 3 frases.",
                    system="Experto en ventas B2B. Emails breves y directos en español.",
                )
                if s_raw:
                    sugerencia = f"\n\n🤖 <b>Respuesta sugerida:</b>\n<i>{s_raw}</i>"
            send_telegram_text(
                f"🔥 <b>LEAD CALIENTE</b>\n\n"
                f"📧 {email_val}\n→ Ha respondido mostrando interés\n→ <b>Respóndele ahora</b>" + sugerencia
            )
        elif estado_nuevo == "BAJA":
            send_telegram_text(f"🚫 <b>Baja registrada</b>\n\n📧 {email_val}\n→ No se le contactará más.")

    # ── Estadísticas finales ──────────────────────────────────────────────────
    total_replied = len(already_replied)
    pending_final = len([r for r in records
                         if not str(r.get(COL_REPLIED, "")).strip()
                         and str(r.get(COL_EMAIL, "")).strip()])

    # Actualizar estado compartido para el chat
    _state["last_run"]      = datetime.now()
    _state["last_sent"]     = sent
    _state["last_errors"]   = errors
    _state["total"]         = total
    _state["contacted"]     = len(contacted_emails)
    _state["pending"]       = pending_final
    _state["total_replied"] = total_replied
    _state["hot_leads"]     = _state.get("hot_leads", 0) + hot_count
    _state["last_replies"]  = reply_count

    log.info(f"Ciclo terminado — Enviados: {sent} | Respuestas nuevas: {reply_count} | "
             f"Total respondido: {total_replied}")

    # ── Reporte texto por Telegram ────────────────────────────────────────────
    text_report = (
        "📊 <b>Reporte semanal – Agente Email Marketing</b>\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"✉️ Emails enviados esta semana: <b>{sent}</b>\n"
        f"❌ Errores: <b>{errors}</b>\n\n"
        f"💬 Respuestas nuevas detectadas: <b>{reply_count}</b>\n"
        f"🔥 HOT leads este ciclo: <b>{hot_count}</b>\n"
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
        new_replies=reply_count, total_replied=total_replied,
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
    log.info(f"  Groq IA: {'activa ✓' if GROQ_API_KEY else 'no configurada'}")
    log.info("═══════════════════════════════════════════════════")

    # Arrancar hilo de escucha de mensajes Telegram
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        t = threading.Thread(target=_polling_loop, daemon=True, name="tg-poll")
        t.start()
        log.info("Hilo de chat Telegram arrancado.")
        threading.Thread(target=_briefing_loop, daemon=True, name="briefing").start()

    _tg_menu(
        "🚀 <b>Agente de Email Marketing arrancado</b>\n"
        f"Revisaré el Sheet cada <b>{CHECK_INTERVAL_H}h</b>, "
        f"detectaré respuestas automáticamente y te enviaré un reporte de texto y audio.\n\n"
        "📱 Usa los botones o escríbeme directamente:"
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
