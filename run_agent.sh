#!/bin/bash
# Crypto Market Intelligence Agent - Haiku 4.5 Runner
# Execute daily market analysis with cost-optimized Haiku model

cd /home/user/Agents

# Log execution
{
  echo "[$(date -u +'%Y-%m-%d %H:%M:%S UTC')] Starting Crypto Market Intelligence Agent (Haiku 4.5)..."

  # Set model to Haiku for this invocation
  export CLAUDE_MODEL=claude-haiku-4-5-20251001

  # Run the agent via claude CLI with the CLAUDE.md instructions
  # This assumes claude CLI is installed and authenticated
  claude --model claude-haiku-4-5-20251001 -p "Execute the crypto market intelligence agent. Follow all steps in CLAUDE.md. Return updated state.json and formatted daily report."

  echo "[$(date -u +'%Y-%m-%d %H:%M:%S UTC')] Agent run complete."
} >> cron.log 2>&1
