#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_cron.sh  –  Instala un cron job para ejecutar el mailer de MineRun
#
# Uso:
#   bash setup_cron.sh           # Cada lunes a las 9:00 (por defecto)
#   bash setup_cron.sh daily     # Cada día a las 9:00
#   bash setup_cron.sh weekly    # Cada lunes a las 9:00
#   bash setup_cron.sh remove    # Elimina el cron job
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
MAILER="$SCRIPT_DIR/mailer.py"
LOG="$SCRIPT_DIR/mailer.log"

MODE="${1:-weekly}"

# Construye la expresión cron
case "$MODE" in
  daily)  CRON_EXPR="0 9 * * *" ;;
  weekly) CRON_EXPR="0 9 * * 1" ;;   # Lunes a las 9:00
  remove)
    crontab -l 2>/dev/null | grep -v "mailer.py" | crontab -
    echo "Cron job eliminado."
    exit 0
    ;;
  *)
    echo "Uso: $0 [daily|weekly|remove]"
    exit 1
    ;;
esac

CRON_LINE="$CRON_EXPR $PYTHON $MAILER >> $LOG 2>&1"

# Añade la línea sólo si no existe ya
( crontab -l 2>/dev/null | grep -v "mailer.py"; echo "$CRON_LINE" ) | crontab -

echo "Cron job instalado ($MODE):"
echo "  $CRON_LINE"
echo ""
echo "Para ver todos tus cron jobs:  crontab -l"
echo "Para eliminar este job:        bash setup_cron.sh remove"
