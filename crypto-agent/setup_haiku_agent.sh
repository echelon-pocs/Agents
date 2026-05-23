#!/bin/bash
# Setup script for Haiku 4.5 agent automation

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$AGENTS_DIR/.env"

echo "═════════════════════════════════════════════════════════"
echo "Crypto Market Intelligence Agent - Haiku 4.5 Setup"
echo "═════════════════════════════════════════════════════════"
echo "Agent directory: $AGENTS_DIR"
echo ""

# Check if ANTHROPIC_API_KEY is already in .env
if grep -q "ANTHROPIC_API_KEY" "$ENV_FILE" 2>/dev/null; then
    echo "✓ ANTHROPIC_API_KEY already configured in .env"
else
    echo "⚠ ANTHROPIC_API_KEY not found in .env"
    echo ""
    echo "To use the agent, add your API key to $ENV_FILE:"
    echo ""
    echo "  echo 'ANTHROPIC_API_KEY=sk-...' >> $ENV_FILE"
    echo ""
    echo "Or set it as an environment variable:"
    echo "  export ANTHROPIC_API_KEY='sk-...'"
    echo ""
fi

# Check if Python script exists
if [ -f "$AGENTS_DIR/run_agent_haiku.py" ]; then
    echo "✓ Python agent script found"
else
    echo "✗ Python agent script not found"
    exit 1
fi

# Check if anthropic SDK is installed
if python3 -c "import anthropic" 2>/dev/null; then
    echo "✓ Anthropic Python SDK installed"
else
    echo "Installing Anthropic Python SDK..."
    python3 -m pip install anthropic --quiet
fi

# Check if requests is installed (needed for telegram_bot.py)
if python3 -c "import requests" 2>/dev/null; then
    echo "✓ requests library installed"
else
    echo "Installing requests..."
    python3 -m pip install requests --quiet
fi

echo ""
echo "To test the agent, run:"
echo "  python3 $AGENTS_DIR/run_agent_haiku.py"
echo ""
echo "Synology Task Scheduler — Daily agent (08:00):"
echo "  python3 $AGENTS_DIR/run_agent_haiku.py >> $AGENTS_DIR/cron.log 2>&1"
echo ""
echo "Synology Task Scheduler — Telegram bot (every 5 min):"
echo "  python3 $AGENTS_DIR/telegram_bot.py >> $AGENTS_DIR/telegram.log 2>&1"
echo ""
