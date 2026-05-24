#!/usr/bin/env bash
# Sets up a daily cron job to run the property search at 08:00 every morning.
# Run once: bash setup_cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
LOG="$SCRIPT_DIR/data/cron.log"
CRON_LINE="0 8 * * * $PYTHON $SCRIPT_DIR/main.py >> $LOG 2>&1"

# Check .env exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env not found. Copy .env.example to .env and fill in credentials."
    exit 1
fi

# Check dependencies are installed
if ! "$PYTHON" -c "import requests, bs4, dotenv" 2>/dev/null; then
    echo "Installing dependencies…"
    "$PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi

# Add cron job if not already present
TMPFILE=$(mktemp)
crontab -l 2>/dev/null | grep -v "ermesinde-property-search" > "$TMPFILE" || true
echo "$CRON_LINE" >> "$TMPFILE"
crontab "$TMPFILE"
rm "$TMPFILE"

echo "✓ Cron job installed: runs daily at 08:00"
echo "  Log file: $LOG"
echo ""
echo "To run manually right now:"
echo "  cd $SCRIPT_DIR && python3 main.py"
echo ""
echo "To run a dry-run (no email):"
echo "  cd $SCRIPT_DIR && python3 main.py --dry-run"
echo ""
echo "To test email sending:"
echo "  cd $SCRIPT_DIR && python3 main.py --test-email"
