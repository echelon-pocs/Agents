#!/bin/bash
# Portfolio Agent — cron setup
# Run once after git pull: bash setup.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON=python3

echo "=== Portfolio Agent Setup ==="
echo "Script dir: $SCRIPT_DIR"

# .env: reuse crypto-agent's if not present
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    if [ -f "$SCRIPT_DIR/../crypto-agent/.env" ]; then
        echo "Symlinking .env from crypto-agent..."
        ln -s "$SCRIPT_DIR/../crypto-agent/.env" "$SCRIPT_DIR/.env"
    else
        echo "WARNING: no .env found. Create $SCRIPT_DIR/.env with:"
        echo "  ANTHROPIC_API_KEY=sk-ant-..."
        echo "  SMTP_HOST=smtp.gmail.com"
        echo "  SMTP_PORT=587"
        echo "  SMTP_USER=your@email.com"
        echo "  SMTP_PASS=your_app_password"
        echo "  ALERT_EMAIL=your@email.com"
    fi
fi

# Cron entry — runs at 08:30 UTC daily (30min after crypto agent)
CRON_LINE="30 8 * * * $PYTHON $SCRIPT_DIR/run_agent.py >> $SCRIPT_DIR/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "portfolio-agent/run_agent.py"; echo "$CRON_LINE" ) | crontab -
echo "Cron job added: $CRON_LINE"

echo "=== Setup complete ==="
echo ""
echo "Test run: $PYTHON $SCRIPT_DIR/run_agent.py"
echo ""
echo "Add IBKR positions via Telegram:"
echo "  /enter VWCE long 158.50"
echo "  /enter VWRL long 155.20"
echo "  /enter 4GLD long 122.00"
echo "  /enter 8PSB long 58.00"
