#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh  –  Configura y arranca el mailer de MineRun en un solo paso
#
# Uso (pasar las claves como variables de entorno):
#
#   SMTP_USER="tu@empresa.com"  \
#   SMTP_PASS="contraseña"      \
#   SHEET_ID="ID_del_sheet"     \
#   SHEET_NAME="Clientes"       \
#   bash setup.sh [--cron daily|weekly]
#
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colores ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC}  $*"; }
warn() { echo -e "${YELLOW}!${NC}  $*"; }
fail() { echo -e "${RED}✗${NC}  $*"; exit 1; }

# ── Parámetros opcionales ────────────────────────────────────────────────────
CRON_MODE=""
for arg in "$@"; do
  case "$arg" in
    --cron) shift; CRON_MODE="$1"; shift ;;
  esac
done

# ── 1. Validar variables obligatorias ────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo "   MineRun Mailer – Setup automático"
echo "════════════════════════════════════════"
echo ""

for var in SMTP_USER SMTP_PASS SHEET_ID; do
  [ -z "${!var:-}" ] && fail "Falta la variable $var. Ejecútalo de nuevo con todas las claves."
done

# ── 2. Generar .env ───────────────────────────────────────────────────────────
cat > .env <<EOF
# Generado por setup.sh el $(date '+%Y-%m-%d %H:%M')
SMTP_HOST=${SMTP_HOST:-correo.hostalia.com}
SMTP_PORT=${SMTP_PORT:-465}
SMTP_USER=${SMTP_USER}
SMTP_PASS=${SMTP_PASS}
FROM_NAME=${FROM_NAME:-MineRun}

GOOGLE_SHEET_ID=${SHEET_ID}
GOOGLE_SHEET_NAME=${SHEET_NAME:-Clientes}
GOOGLE_CREDENTIALS_FILE=credentials.json

COL_EMAIL=${COL_EMAIL:-Email}
COL_PRODUCT=${COL_PRODUCT:-Producto}
EOF
ok ".env creado"

# ── 3. Instalar dependencias ──────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  fail "Python 3 no encontrado. Instálalo primero."
fi

python3 -m pip install --quiet -r requirements.txt
ok "Dependencias instaladas"

# ── 4. Verificar credentials.json ────────────────────────────────────────────
if [ ! -f credentials.json ]; then
  warn "credentials.json no encontrado."
  warn "Necesitas subirlo manualmente (Service Account de Google)."
  warn "El mailer fallará hasta que esté disponible."
else
  ok "credentials.json encontrado"
fi

# ── 5. Test de conexión SMTP ──────────────────────────────────────────────────
echo ""
echo "Probando conexión SMTP…"
python3 - <<PYEOF
import os, smtplib, ssl
from dotenv import load_dotenv
load_dotenv()
host = os.getenv('SMTP_HOST'); port = int(os.getenv('SMTP_PORT', 465))
user = os.getenv('SMTP_USER'); pwd  = os.getenv('SMTP_PASS')
try:
    if port == 587:
        s = smtplib.SMTP(host, port, timeout=10); s.starttls()
    else:
        s = smtplib.SMTP_SSL(host, port, timeout=10)
    s.login(user, pwd); s.quit()
    print("SMTP OK")
except Exception as e:
    print(f"SMTP ERROR: {e}")
    exit(1)
PYEOF
ok "Conexión SMTP verificada"

# ── 6. Dry-run del mailer ─────────────────────────────────────────────────────
echo ""
echo "Ejecutando dry-run (sin enviar correos)…"
python3 mailer.py --dry-run

# ── 7. Instalar cron (opcional) ───────────────────────────────────────────────
if [ -n "$CRON_MODE" ]; then
  bash setup_cron.sh "$CRON_MODE"
  ok "Cron instalado ($CRON_MODE)"
fi

echo ""
echo "════════════════════════════════════════"
ok "Setup completado."
echo ""
echo "Para enviar los correos ahora:"
echo "  python3 mailer.py"
echo ""
echo "Para activar el envío automático semanal:"
echo "  bash setup_cron.sh weekly"
echo "════════════════════════════════════════"
echo ""
