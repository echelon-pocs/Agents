#!/bin/bash
# Setup script for Haiku 4.5 agent automation

echo "═══════════════════════════════════════════════════════════"
echo "Crypto Market Intelligence Agent - Haiku 4.5 Setup"
echo "═══════════════════════════════════════════════════════════"
echo ""

ENV_FILE="/home/user/Agents/.env"

# Check if ANTHROPIC_API_KEY is already in .env
if grep -q "ANTHROPIC_API_KEY" "$ENV_FILE"; then
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
if [ -f "/home/user/Agents/run_agent_haiku.py" ]; then
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
    pip3 install anthropic --quiet
fi

# Offer to test the script
echo ""
echo "To test the agent with Haiku 4.5, run:"
echo "  python3 /home/user/Agents/run_agent_haiku.py"
echo ""
echo "To schedule daily runs:"
echo "  • Synology: Task Scheduler > Create > Run script"
echo "    - Path: /home/user/Agents/run_agent_haiku.py"
echo "    - Schedule: Daily at 08:00"
echo ""
echo "  • Linux/macOS: Add to crontab"
echo "    - 0 8 * * * /usr/bin/python3 /home/user/Agents/run_agent_haiku.py"
echo ""
